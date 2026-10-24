// Package credential reads application credentials from Vault, independently of
// policy discovery and session persistence. Values stay in control-plane memory.
package credential

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	vault "github.com/hashicorp/vault/api"
	"golang.org/x/sync/singleflight"
)

type Provider interface {
	Read(context.Context, string, string) (string, error)
	Invalidate(string, string) error
	Stats() map[string]uint64
}

type Config struct {
	Address, CACert, Namespace string
	Mount, Prefix, Field       string
	KubernetesNamespace        string
	AuthMount, Role, JWTPath   string
	TokenFile                  string
}

type Vault struct {
	client                             *vault.Client
	config                             Config
	authMu                             sync.Mutex
	expires                            time.Time
	mu                                 sync.Mutex
	values                             map[string]string
	versions                           map[string]uint64
	loads                              singleflight.Group
	reads, logins, hits, invalidations atomic.Uint64
}

func FromEnv() (*Vault, error) {
	return New(Config{
		Address: os.Getenv("VAULT_ADDR"), CACert: os.Getenv("VAULT_CACERT"), Namespace: os.Getenv("VAULT_NAMESPACE"),
		Mount: env("VAULT_KV_MOUNT", "kv"), Prefix: env("VAULT_KV_PREFIX", "auth-demo"), Field: env("VAULT_KV_FIELD", "token"),
		KubernetesNamespace: env("DEMO_NAMESPACE", "demo"), AuthMount: env("VAULT_AUTH_MOUNT", "kubernetes"),
		Role: os.Getenv("VAULT_ROLE"), JWTPath: env("VAULT_JWT_PATH", "/var/run/vault-identity/token"), TokenFile: os.Getenv("VAULT_TOKEN_FILE"),
	})
}

func New(c Config) (*Vault, error) {
	u, err := url.Parse(c.Address)
	if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return nil, fmt.Errorf("VAULT_ADDR must be an HTTPS origin")
	}
	for _, value := range []string{c.Mount, c.Prefix, c.KubernetesNamespace, c.Field} {
		if !validPath(value) {
			return nil, fmt.Errorf("invalid Vault mount, prefix, namespace or field")
		}
	}
	if c.TokenFile == "" && (!validPath(c.AuthMount) || c.Role == "" || c.JWTPath == "") {
		return nil, fmt.Errorf("Vault Kubernetes auth requires mount, role and JWT path")
	}
	config := vault.DefaultConfig()
	config.Address = strings.TrimRight(c.Address, "/")
	config.Timeout = 5 * time.Second
	config.MaxRetries = 0
	if err = config.ConfigureTLS(&vault.TLSConfig{CACert: c.CACert}); err != nil {
		return nil, fmt.Errorf("configure Vault TLS: %w", err)
	}
	client, err := vault.NewClient(config)
	if err != nil {
		return nil, fmt.Errorf("configure Vault client: %w", err)
	}
	if c.Namespace != "" {
		client.SetNamespace(c.Namespace)
	}
	return &Vault{client: client, config: c, values: map[string]string{}, versions: map[string]uint64{}}, nil
}

func validPath(value string) bool {
	if value == "" || len(value) > 512 {
		return false
	}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." || part == ".." {
			return false
		}
		for _, c := range part {
			if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '_' || c == '.') {
				return false
			}
		}
	}
	return true
}

func (v *Vault) key(namespace, name string) (string, error) {
	if namespace != v.config.KubernetesNamespace || !validPath(name) {
		return "", fmt.Errorf("credential is outside the configured Vault scope")
	}
	return namespace + "/" + name, nil
}

// Read caches the first successful KV-v2 value without a timer. Rotation is
// explicit: invalidate after writing Vault, then the next approved request reads
// the new value. Generations keep old in-flight reads from repopulating the cache.
func (v *Vault) Read(ctx context.Context, namespace, name string) (string, error) {
	key, err := v.key(namespace, name)
	if err != nil {
		return "", err
	}
	v.mu.Lock()
	value, ok := v.values[key]
	generation := v.versions[key]
	v.mu.Unlock()
	if ok {
		v.hits.Add(1)
		return value, nil
	}
	ch := v.loads.DoChan(fmt.Sprintf("%s#%d", key, generation), func() (any, error) {
		v.mu.Lock()
		cached, exists := v.values[key]
		current := v.versions[key]
		v.mu.Unlock()
		if current != generation {
			return "", fmt.Errorf("credential invalidated during Vault read")
		}
		if exists {
			v.hits.Add(1)
			return cached, nil
		}
		readCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
		defer cancel()
		value, err := v.read(readCtx, key)
		if err != nil {
			return "", err
		}
		v.mu.Lock()
		defer v.mu.Unlock()
		if v.versions[key] != generation {
			return "", fmt.Errorf("credential invalidated during Vault read")
		}
		if _, exists := v.values[key]; !exists && len(v.values) >= 128 {
			return "", fmt.Errorf("Vault credential cache limit reached")
		}
		v.values[key] = value
		return value, nil
	})
	select {
	case <-ctx.Done():
		return "", ctx.Err()
	case result := <-ch:
		if result.Err != nil {
			return "", result.Err
		}
		return result.Val.(string), nil
	}
}

func (v *Vault) authenticate(ctx context.Context, force bool) error {
	v.authMu.Lock()
	defer v.authMu.Unlock()
	if !force && v.client.Token() != "" && time.Now().Before(v.expires) {
		return nil
	}
	if v.config.TokenFile != "" {
		raw, err := os.ReadFile(v.config.TokenFile)
		if err != nil || strings.TrimSpace(string(raw)) == "" {
			return fmt.Errorf("Vault token file unavailable")
		}
		v.client.SetToken(strings.TrimSpace(string(raw)))
		// Re-read external token sinks on each cache miss; the owning Vault Agent
		// manages their lifetime. A token never enters our Kubernetes manifests.
		v.expires = time.Time{}
		return nil
	}
	jwt, err := os.ReadFile(v.config.JWTPath)
	if err != nil || strings.TrimSpace(string(jwt)) == "" {
		return fmt.Errorf("Vault service-account JWT unavailable")
	}
	v.logins.Add(1)
	secret, err := v.client.Logical().WriteWithContext(ctx, "auth/"+v.config.AuthMount+"/login", map[string]any{"role": v.config.Role, "jwt": strings.TrimSpace(string(jwt))})
	if err != nil {
		return sanitized("login", err)
	}
	if secret == nil || secret.Auth == nil || secret.Auth.ClientToken == "" || secret.Auth.LeaseDuration <= 0 {
		return fmt.Errorf("Vault login returned no usable token")
	}
	v.client.SetToken(secret.Auth.ClientToken)
	v.expires = time.Now().Add(time.Duration(secret.Auth.LeaseDuration) * time.Second * 4 / 5)
	return nil
}

func (v *Vault) read(ctx context.Context, key string) (string, error) {
	for attempt := 0; attempt < 2; attempt++ {
		if err := v.authenticate(ctx, attempt > 0); err != nil {
			return "", err
		}
		v.reads.Add(1)
		secret, err := v.client.Logical().ReadWithContext(ctx, v.config.Mount+"/data/"+v.config.Prefix+"/"+key)
		if err != nil {
			var response *vault.ResponseError
			if attempt == 0 && errors.As(err, &response) && (response.StatusCode == 401 || response.StatusCode == 403) {
				continue
			}
			return "", sanitized("read", err)
		}
		if secret == nil {
			return "", fmt.Errorf("Vault credential not found")
		}
		data, ok := secret.Data["data"].(map[string]any)
		if !ok {
			return "", fmt.Errorf("Vault returned no KV-v2 credential data")
		}
		value, ok := data[v.config.Field].(string)
		if !ok || value == "" || strings.ContainsAny(value, "\r\n") {
			return "", fmt.Errorf("Vault credential field is empty or invalid")
		}
		return value, nil
	}
	return "", fmt.Errorf("Vault authentication failed")
}

func sanitized(operation string, err error) error {
	var response *vault.ResponseError
	if errors.As(err, &response) {
		return fmt.Errorf("Vault %s failed (HTTP %d)", operation, response.StatusCode)
	}
	return fmt.Errorf("Vault %s unavailable", operation)
}

func (v *Vault) Invalidate(namespace, name string) error {
	key, err := v.key(namespace, name)
	if err != nil {
		return err
	}
	v.mu.Lock()
	delete(v.values, key)
	v.versions[key]++
	v.mu.Unlock()
	v.invalidations.Add(1)
	return nil
}

func (v *Vault) Stats() map[string]uint64 {
	return map[string]uint64{"vaultReads": v.reads.Load(), "vaultLogins": v.logins.Load(), "credentialCacheHits": v.hits.Load(), "credentialInvalidations": v.invalidations.Load()}
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

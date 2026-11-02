// Package credential resolves credentials mounted only into one project's authorizer.
package credential

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"example.com/auth-demo/pkg/authstate"
)

type Provider interface {
	Read(context.Context, string, string) (string, error)
	Invalidate(string, string) error
	Stats() map[string]uint64
}

type Entry struct {
	Type          string   `json:"type"`
	Token         string   `json:"token,omitempty"`
	TokenEndpoint string   `json:"tokenEndpoint,omitempty"`
	ClientID      string   `json:"clientId,omitempty"`
	ClientSecret  string   `json:"clientSecret,omitempty"`
	Scopes        []string `json:"scopes,omitempty"`
	Audience      string   `json:"audience,omitempty"`
}
type Mounted struct {
	Namespace, Project, File string
	HTTP                     *http.Client
	Shared                   *authstate.Cache
	mu                       sync.Mutex
	digest                   [32]byte
	entries                  map[string]Entry
	generation               uint64
	reads, hits, exchanges   atomic.Uint64
}

func NewMounted(namespace, project, file string) (*Mounted, error) {
	if namespace == "" || project == "" || file == "" {
		return nil, fmt.Errorf("credential namespace, project and mounted file are required")
	}
	m := &Mounted{Namespace: namespace, Project: project, File: file, HTTP: &http.Client{Timeout: 8 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return fmt.Errorf("credential endpoint redirect denied") }}}
	if err := m.reload(); err != nil {
		return nil, err
	}
	return m, nil
}
func (m *Mounted) reload() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	// A projected Secret replaces ..data atomically. Open its single JSON file afresh;
	// this cannot mix old and new client ID/secret fields and needs no API polling.
	f, err := os.Open(m.File)
	if err != nil {
		return fmt.Errorf("mounted credentials unavailable")
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, (1<<20)+1))
	if err != nil || len(raw) > 1<<20 {
		return fmt.Errorf("mounted credential document unavailable or too large")
	}
	digest := sha256.Sum256(raw)
	if m.entries != nil && digest == m.digest {
		return nil
	}
	entries := map[string]Entry{}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&entries); err != nil {
		return fmt.Errorf("invalid mounted credential document")
	}
	if err := dec.Decode(new(any)); err != io.EOF {
		return fmt.Errorf("invalid trailing credential data")
	}
	for name, e := range entries {
		if name == "" || strings.ContainsAny(name, "/\\\r\n") {
			return fmt.Errorf("invalid credential reference")
		}
		switch e.Type {
		case "bearer":
			if e.Token == "" || strings.ContainsAny(e.Token, "\r\n") {
				return fmt.Errorf("invalid bearer credential")
			}
		case "oauth2":
			u, err := url.Parse(e.TokenEndpoint)
			if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.Fragment != "" || e.ClientID == "" || e.ClientSecret == "" {
				return fmt.Errorf("invalid OAuth client credentials configuration")
			}
		default:
			return fmt.Errorf("unsupported credential resolver")
		}
	}
	m.entries = entries
	m.digest = digest
	m.generation++
	m.reads.Add(1)
	return nil
}
func (m *Mounted) Read(ctx context.Context, ns, name string) (string, error) {
	if ns != m.Namespace {
		return "", fmt.Errorf("credential belongs to another project scope")
	}
	if err := m.reload(); err != nil {
		return "", err
	}
	m.mu.Lock()
	entry, ok := m.entries[name]
	generation := m.generation
	if !ok {
		m.mu.Unlock()
		return "", fmt.Errorf("credential reference is not mounted for this project")
	}
	if entry.Type == "bearer" {
		m.mu.Unlock()
		m.hits.Add(1)
		return entry.Token, nil
	}
	m.mu.Unlock()
	if m.Shared == nil {
		return "", fmt.Errorf("shared authentication state is required")
	}
	return m.readShared(ctx, name, entry, generation)
}

func (m *Mounted) Invalidate(ns, name string) error {
	if ns != m.Namespace {
		return fmt.Errorf("credential belongs to another project scope")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	entry, ok := m.entries[name]
	if !ok {
		return fmt.Errorf("unknown credential")
	}
	m.generation++
	if m.Shared != nil && entry.Type == "oauth2" {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return m.Shared.Invalidate(ctx, m.sharedKey(name, entry))
	}
	return nil
}
func (m *Mounted) Stats() map[string]uint64 {
	return map[string]uint64{"credentialFileLoads": m.reads.Load(), "credentialCacheHits": m.hits.Load(), "oauthExchanges": m.exchanges.Load()}
}

func (m *Mounted) acquire(ctx context.Context, entry Entry) (authstate.Token, error) {
	values := url.Values{"grant_type": {"client_credentials"}}
	if len(entry.Scopes) > 0 {
		values.Set("scope", strings.Join(entry.Scopes, " "))
	}
	if entry.Audience != "" {
		values.Set("audience", entry.Audience)
	}
	req, err := http.NewRequestWithContext(ctx, "POST", entry.TokenEndpoint, strings.NewReader(values.Encode()))
	if err != nil {
		return authstate.Token{}, fmt.Errorf("token request unavailable")
	}
	req.SetBasicAuth(entry.ClientID, entry.ClientSecret)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	m.exchanges.Add(1)
	resp, err := m.HTTP.Do(req)
	if err != nil {
		return authstate.Token{}, fmt.Errorf("token service unavailable")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return authstate.Token{}, fmt.Errorf("token service rejected authentication")
	}
	var out struct {
		AccessToken string `json:"access_token"`
		TokenType   string `json:"token_type"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&out); err != nil || out.AccessToken == "" || strings.ContainsAny(out.AccessToken, "\r\n") || !strings.EqualFold(out.TokenType, "Bearer") || out.ExpiresIn <= 0 || out.ExpiresIn > 365*24*3600 {
		return authstate.Token{}, fmt.Errorf("invalid token response")
	}

	lifetime := time.Duration(out.ExpiresIn) * time.Second
	refresh := lifetime / 10
	if refresh > 30*time.Second {
		refresh = 30 * time.Second
	}
	return authstate.Token{Value: out.AccessToken, Expires: time.Now().Add(lifetime - refresh)}, nil
}

func (m *Mounted) sharedKey(name string, entry Entry) string {
	raw, _ := json.Marshal(entry)
	return m.Shared.Key(m.Namespace, m.Project, name, string(raw))
}

func (m *Mounted) readShared(ctx context.Context, name string, entry Entry, generation uint64) (string, error) {
	value, err := m.Shared.Get(ctx, m.sharedKey(name, entry), func(fetch context.Context) (authstate.Token, error) {
		value, err := m.acquire(fetch, entry)
		if err != nil {
			return authstate.Token{}, err
		}
		if err = m.reload(); err != nil {
			return authstate.Token{}, err
		}
		m.mu.Lock()
		defer m.mu.Unlock()
		if generation != m.generation {
			return authstate.Token{}, fmt.Errorf("credential rotated during token acquisition")
		}
		return value, nil
	})
	if err != nil {
		return "", err
	}
	// Also check rotations when Get reused state instead of invoking acquisition.
	if err = m.reload(); err != nil {
		return "", err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if generation != m.generation {
		return "", fmt.Errorf("credential rotated")
	}
	m.hits.Add(1)
	return value.Value, nil
}

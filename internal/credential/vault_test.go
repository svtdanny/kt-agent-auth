package credential

import (
	"context"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

type vaultFixture struct {
	mu             sync.Mutex
	value          string
	fail           bool
	rejectReads    int
	block, entered chan struct{}
}

func fixture(t *testing.T) (*Vault, *vaultFixture, *httptest.Server) {
	t.Helper()
	f := &vaultFixture{value: "synthetic-key-v1"}
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Vault-Namespace") != "team" {
			t.Error("Vault namespace header missing")
		}
		if r.URL.Path == "/v1/auth/kubernetes/login" {
			var input map[string]string
			_ = json.NewDecoder(r.Body).Decode(&input)
			if input["jwt"] != "synthetic-service-account-jwt" || input["role"] != "bridge" {
				t.Error("Kubernetes login did not use configured identity")
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"auth": map[string]any{"client_token": "synthetic-vault-token", "lease_duration": 3600}})
			return
		}
		if r.Header.Get("X-Vault-Token") != "synthetic-vault-token" {
			t.Error("Vault read was not authenticated")
		}
		f.mu.Lock()
		value, fail, block, entered := f.value, f.fail, f.block, f.entered
		reject := f.rejectReads > 0
		if reject {
			f.rejectReads--
		}
		f.mu.Unlock()
		if reject {
			http.Error(w, "rejected-token-DO-NOT-LEAK", http.StatusForbidden)
			return
		}
		if entered != nil {
			close(entered)
			<-block
		}
		if fail {
			http.Error(w, "DO-NOT-LEAK-SECRET", 503)
			return
		}
		if r.URL.Path != "/v1/kv/data/auth-demo/demo/read-token" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"data": map[string]any{"token": value}, "metadata": map[string]any{"version": 1}}})
	}))
	t.Cleanup(server.Close)
	dir := t.TempDir()
	ca := filepath.Join(dir, "ca.pem")
	jwt := filepath.Join(dir, "jwt")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(jwt, []byte("synthetic-service-account-jwt"), 0600); err != nil {
		t.Fatal(err)
	}
	v, err := New(Config{Address: server.URL, CACert: ca, Namespace: "team", Mount: "kv", Prefix: "auth-demo", Field: "token", KubernetesNamespace: "demo", AuthMount: "kubernetes", Role: "bridge", JWTPath: jwt})
	if err != nil {
		t.Fatal(err)
	}
	return v, f, server
}

func TestVaultReauthenticatesAfterRejectedToken(t *testing.T) {
	v, f, _ := fixture(t)
	f.rejectReads = 1
	value, err := v.Read(context.Background(), "demo", "read-token")
	if err != nil || value != "synthetic-key-v1" || v.Stats()["vaultReads"] != 2 || v.Stats()["vaultLogins"] != 2 {
		t.Fatal("rejected token did not trigger one bounded Kubernetes reauthentication")
	}
	if err = v.Invalidate("demo", "read-token"); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	f.rejectReads = 10
	f.mu.Unlock()
	_, err = v.Read(context.Background(), "demo", "read-token")
	if err == nil || strings.Contains(err.Error(), "DO-NOT-LEAK") || v.Stats()["vaultReads"] != 4 {
		t.Fatal("persistent denial did not fail closed after two attempts")
	}
}

func TestVaultAcceptsExternallyManagedTokenSink(t *testing.T) {
	base, _, _ := fixture(t)
	path := filepath.Join(t.TempDir(), "vault-token")
	if err := os.WriteFile(path, []byte("synthetic-vault-token\n"), 0600); err != nil {
		t.Fatal(err)
	}
	config := base.config
	config.TokenFile = path
	config.Role = ""
	config.JWTPath = ""
	v, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = v.Read(context.Background(), "demo", "read-token"); err != nil || v.Stats()["vaultLogins"] != 0 {
		t.Fatal("external token sink was not used")
	}
	if err = os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err = v.Invalidate("demo", "read-token"); err != nil {
		t.Fatal(err)
	}
	if _, err = v.Read(context.Background(), "demo", "read-token"); err == nil {
		t.Fatal("unavailable token sink used an old credential")
	}
}

func TestVaultConcurrentFirstReadAndExplicitRotation(t *testing.T) {
	v, f, _ := fixture(t)
	var wg sync.WaitGroup
	for range 32 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			value, err := v.Read(context.Background(), "demo", "read-token")
			if err != nil || value != "synthetic-key-v1" {
				t.Error("first read failed")
			}
		}()
	}
	wg.Wait()
	if v.Stats()["vaultReads"] != 1 || v.Stats()["vaultLogins"] != 1 {
		t.Fatalf("concurrent misses were not coalesced: %v", v.Stats())
	}
	f.mu.Lock()
	f.value = "synthetic-key-v2"
	f.mu.Unlock()
	value, _ := v.Read(context.Background(), "demo", "read-token")
	if value != "synthetic-key-v1" {
		t.Fatal("unexpected scheduled refresh")
	}
	if err := v.Invalidate("demo", "read-token"); err != nil {
		t.Fatal(err)
	}
	value, err := v.Read(context.Background(), "demo", "read-token")
	if err != nil || value != "synthetic-key-v2" || v.Stats()["vaultReads"] != 2 {
		t.Fatal("invalidation did not fetch new Vault value")
	}
}

func TestInvalidationDoesNotRestoreAnInflightStaleRead(t *testing.T) {
	v, f, _ := fixture(t)
	f.block = make(chan struct{})
	f.entered = make(chan struct{})
	done := make(chan error, 1)
	go func() { _, err := v.Read(context.Background(), "demo", "read-token"); done <- err }()
	<-f.entered
	if err := v.Invalidate("demo", "read-token"); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	f.value = "new-key"
	f.mu.Unlock()
	close(f.block)
	if err := <-done; err == nil {
		t.Fatal("stale in-flight read survived invalidation")
	}
	f.mu.Lock()
	f.entered = nil
	f.block = nil
	f.mu.Unlock()
	value, err := v.Read(context.Background(), "demo", "read-token")
	if err != nil || value != "new-key" {
		t.Fatal("new generation was not read")
	}
}

func TestVaultScopeErrorsAndDisconnectedCache(t *testing.T) {
	v, f, _ := fixture(t)
	for _, name := range []string{"../other", "/absolute", "a/../../b", "a%2fb", "x?version=2", "a//b"} {
		if _, err := v.Read(context.Background(), "demo", name); err == nil {
			t.Fatal("unsafe Vault path accepted")
		}
	}
	if _, err := v.Read(context.Background(), "other", "read-token"); err == nil {
		t.Fatal("other namespace accepted")
	}
	if v.Stats()["vaultReads"] != 0 {
		t.Fatal("invalid scope contacted Vault")
	}
	if _, err := v.Read(context.Background(), "demo", "read-token"); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	f.fail = true
	f.mu.Unlock()
	if value, err := v.Read(context.Background(), "demo", "read-token"); err != nil || value == "" {
		t.Fatal("known snapshot expired during Vault outage")
	}
	if _, err := v.Read(context.Background(), "demo", "uncached"); err == nil || strings.Contains(err.Error(), "DO-NOT-LEAK") {
		t.Fatal("unresolved miss permitted or Vault response leaked")
	}
	if err := v.Invalidate("demo", "read-token"); err != nil {
		t.Fatal(err)
	}
	if _, err := v.Read(context.Background(), "demo", "read-token"); err == nil {
		t.Fatal("explicit invalidation fell back to an old key")
	}
}

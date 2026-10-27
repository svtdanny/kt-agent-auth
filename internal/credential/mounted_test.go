package credential

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func secret(t *testing.T, p string, entries map[string]Entry) {
	t.Helper()
	raw, _ := json.Marshal(entries)
	tmp := p + ".new"
	if err := os.WriteFile(tmp, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, p); err != nil {
		t.Fatal(err)
	}
}
func TestMountedCredentialsScopeRotationAndInvalidDocuments(t *testing.T) {
	p := filepath.Join(t.TempDir(), "credentials.json")
	secret(t, p, map[string]Entry{"api-token": {Type: "bearer", Token: "agent-a"}})
	a, err := NewMounted("agent-a", "mastra", p)
	if err != nil {
		t.Fatal(err)
	}
	if value, err := a.Read(context.Background(), "agent-a", "api-token"); err != nil || value != "agent-a" {
		t.Fatal(value, err)
	}
	if _, err := a.Read(context.Background(), "agent-b", "api-token"); err == nil {
		t.Fatal("cross-scope credential reuse")
	}
	if _, err := a.Read(context.Background(), "agent-a", "../api-token"); err == nil {
		t.Fatal("credential traversal accepted")
	}
	secret(t, p, map[string]Entry{"api-token": {Type: "bearer", Token: "rotated"}})
	if value, err := a.Read(context.Background(), "agent-a", "api-token"); err != nil || value != "rotated" {
		t.Fatal("rotation not observed", err)
	}
	os.WriteFile(p, []byte(`{"api-token":{"type":"bearer","token":"x","unexpected":true}}`), 0600)
	if _, err := a.Read(context.Background(), "agent-a", "api-token"); err == nil {
		t.Fatal("invalid replacement reused old key")
	}
	secret(t, p, map[string]Entry{})
	if _, err := a.Read(context.Background(), "agent-a", "api-token"); err == nil {
		t.Fatal("removed credential reused")
	}
}
func TestOAuthCacheCoalescingRefreshAndIndependentAgents(t *testing.T) {
	var calls atomic.Int64
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		user, pass, ok := r.BasicAuth()
		r.ParseForm()
		if !ok || user != "client" || pass != "secret" || r.Form.Get("grant_type") != "client_credentials" {
			w.WriteHeader(401)
			return
		}
		time.Sleep(25 * time.Millisecond)
		fmt.Fprint(w, `{"access_token":"scoped-access","token_type":"Bearer","expires_in":1}`)
	}))
	defer server.Close()
	p := filepath.Join(t.TempDir(), "credentials.json")
	secret(t, p, map[string]Entry{"api-token": {Type: "oauth2", TokenEndpoint: server.URL, ClientID: "client", ClientSecret: "secret"}})
	a, err := NewMounted("agent-a", "mastra", p)
	if err != nil {
		t.Fatal(err)
	}
	a.HTTP = server.Client()
	var wg sync.WaitGroup
	for range 32 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			value, err := a.Read(context.Background(), "agent-a", "api-token")
			if err != nil || value != "scoped-access" {
				t.Error("token acquisition failed", err)
			}
		}()
	}
	wg.Wait()
	if calls.Load() != 1 {
		t.Fatal("token endpoint stampede", calls.Load())
	}
	b, err := NewMounted("agent-b", "mastra", p)
	if err != nil {
		t.Fatal(err)
	}
	b.HTTP = server.Client()
	if _, err := b.Read(context.Background(), "agent-b", "api-token"); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 2 {
		t.Fatal("token cache was shared between agents")
	}
	time.Sleep(time.Second)
	if _, err := a.Read(context.Background(), "agent-a", "api-token"); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 3 {
		t.Fatal("token expiry did not refresh")
	}
	secret(t, p, map[string]Entry{"api-token": {Type: "oauth2", TokenEndpoint: server.URL, ClientID: "client", ClientSecret: "wrong"}})
	if _, err := a.Read(context.Background(), "agent-a", "api-token"); err == nil {
		t.Fatal("rotated wrong secret reused token")
	}
}
func TestOAuthRotationDuringAcquisitionCannotPublishOldToken(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-release
		fmt.Fprint(w, `{"access_token":"old","token_type":"Bearer","expires_in":3600}`)
	}))
	defer server.Close()
	p := filepath.Join(t.TempDir(), "credentials.json")
	secret(t, p, map[string]Entry{"api-token": {Type: "oauth2", TokenEndpoint: server.URL, ClientID: "client", ClientSecret: "secret"}})
	a, _ := NewMounted("agent-a", "mastra", p)
	a.HTTP = server.Client()
	done := make(chan error, 1)
	go func() { _, err := a.Read(context.Background(), "agent-a", "api-token"); done <- err }()
	<-started
	secret(t, p, map[string]Entry{"api-token": {Type: "bearer", Token: "new"}})
	close(release)
	if err := <-done; err == nil {
		t.Fatal("old token released after rotation")
	}
	if value, err := a.Read(context.Background(), "agent-a", "api-token"); err != nil || value != "new" {
		t.Fatal(value, err)
	}
}

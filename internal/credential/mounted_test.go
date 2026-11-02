package credential

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"example.com/auth-demo/pkg/authstate"
	"github.com/jackc/pgx/v5/pgxpool"
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

func TestSharedOAuthReplicasAndCredentialRotation(t *testing.T) {
	url := os.Getenv("AUTH_DEMO_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("requires isolated PostgreSQL integration database")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	var calls atomic.Int64
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		_, pass, _ := r.BasicAuth()
		fmt.Fprintf(w, `{"access_token":%q,"token_type":"Bearer","expires_in":3600}`, "derived-"+pass)
	}))
	defer server.Close()
	p := filepath.Join(t.TempDir(), "credentials.json")
	secret(t, p, map[string]Entry{"api-token": {Type: "oauth2", TokenEndpoint: server.URL, ClientID: "client", ClientSecret: "old"}})
	newReplica := func() *Mounted {
		m, e := NewMounted("project-a", "project-a", p)
		if e != nil {
			t.Fatal(e)
		}
		m.HTTP = server.Client()
		m.Shared, e = authstate.New(ctx, pool, bytes.Repeat([]byte{5}, 32))
		if e != nil {
			t.Fatal(e)
		}
		return m
	}
	a, b := newReplica(), newReplica()
	for _, m := range []*Mounted{a, b, newReplica()} {
		if v, e := m.Read(ctx, "project-a", "api-token"); e != nil || v != "derived-old" {
			t.Fatal(v, e)
		}
	}
	if calls.Load() != 1 {
		t.Fatal("replicas/restart repeated login", calls.Load())
	}
	secret(t, p, map[string]Entry{"api-token": {Type: "oauth2", TokenEndpoint: server.URL, ClientID: "client", ClientSecret: "new"}})
	for _, m := range []*Mounted{b, a} {
		if v, e := m.Read(ctx, "project-a", "api-token"); e != nil || v != "derived-new" {
			t.Fatal(v, e)
		}
	}
	if calls.Load() != 2 {
		t.Fatal("rotation was not shared", calls.Load())
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
func TestOAuthProtocolAndRefreshDeadline(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, pass, _ := r.BasicAuth()
		r.ParseForm()
		if user != "client" || pass != "secret" || r.Form.Get("grant_type") != "client_credentials" {
			w.WriteHeader(401)
			return
		}
		fmt.Fprint(w, `{"access_token":"scoped-access","token_type":"Bearer","expires_in":10}`)
	}))
	defer server.Close()
	m := &Mounted{HTTP: server.Client()}
	entry := Entry{Type: "oauth2", TokenEndpoint: server.URL, ClientID: "client", ClientSecret: "secret"}
	before := time.Now()
	v, e := m.acquire(context.Background(), entry)
	if e != nil || v.Value != "scoped-access" || v.Expires.Before(before.Add(8*time.Second)) || v.Expires.After(before.Add(10*time.Second)) {
		t.Fatal("bad token or refresh deadline", e)
	}
	entry.ClientSecret = "wrong"
	if _, e = m.acquire(context.Background(), entry); e == nil {
		t.Fatal("bad client credentials accepted")
	}
}

func TestOAuthRotationDuringAcquisitionCannotPublishOldToken(t *testing.T) {
	url := os.Getenv("AUTH_DEMO_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("requires isolated PostgreSQL integration database")
	}
	pool, e := pgxpool.New(context.Background(), url)
	if e != nil {
		t.Fatal(e)
	}
	defer pool.Close()
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
	a.Shared, e = authstate.New(context.Background(), pool, bytes.Repeat([]byte{7}, 32))
	if e != nil {
		t.Fatal(e)
	}
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

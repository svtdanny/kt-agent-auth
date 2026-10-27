package store

import (
	"context"
	"example.com/auth-demo/internal/api"
	"github.com/google/uuid"
	"os"
	"sync"
	"testing"
	"time"
)

func sqlFixture(t *testing.T) (*Store, string, api.CreateSession, api.Binding) {
	t.Helper()
	url := os.Getenv("AUTH_DEMO_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("run python3 scripts/test_postgres.py for real PostgreSQL integration tests")
	}
	s, err := Open(url)
	if err != nil {
		t.Fatal(err)
	}
	user := "test-" + uuid.NewString()
	r := api.CreateSession{Agent: api.Ref{Namespace: "demo", Name: "mastra-http"}, ServiceAccount: "reader", RequestID: uuid.NewString()}
	b := api.Binding{SessionID: uuid.NewString(), Atespace: "demo", User: user, Agent: r.Agent, Account: api.Ref{Namespace: "demo", Name: "reader"}, AccountUID: "uid", Active: true}
	t.Cleanup(func() {
		_, _ = s.Pool.Exec(context.Background(), `DELETE FROM auth_bridge.launches WHERE caller=$1`, user)
		_, _ = s.Pool.Exec(context.Background(), `DELETE FROM auth_bridge.bindings WHERE data->>'user'=$1`, user)
		_ = s.Close()
	})
	return s, user, r, b
}
func TestPostgresConcurrentIdempotencyAndImmutableBinding(t *testing.T) {
	s, user, r, b := sqlFixture(t)
	var wg sync.WaitGroup
	for range 32 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			l, err := s.Reserve(user, r, "uid")
			if err == nil {
				err = s.Activate(l, b)
			}
			if err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	var count int
	if err := s.Pool.QueryRow(context.Background(), `SELECT count(*) FROM auth_bridge.bindings WHERE session_id=$1`, b.SessionID).Scan(&count); err != nil || count != 1 {
		t.Fatalf("concurrent activation: %d %v", count, err)
	}
	changed := r
	changed.ServiceAccount = "writer"
	if _, err := s.Reserve(user, changed, "uid"); err == nil {
		t.Fatal("account changed on retry")
	}
	if _, err := s.Reserve(user, r, "replacement-uid"); err == nil {
		t.Fatal("UID changed on retry")
	}
	other, err := Open(os.Getenv("AUTH_DEMO_TEST_DATABASE_URL"))
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	got, err := other.Binding(b.SessionID)
	if err != nil || got != b {
		t.Fatalf("fresh connection lost binding: %+v %v", got, err)
	}
	if err = other.Deactivate(b.SessionID); err != nil {
		t.Fatal(err)
	}
	got, err = s.Binding(b.SessionID)
	if err != nil || got.Active {
		t.Fatal("revocation not shared across connections")
	}
}
func TestPostgresRetentionKeepsRequestTombstone(t *testing.T) {
	s, user, r, b := sqlFixture(t)
	l, err := s.Reserve(user, r, "uid")
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Activate(l, b); err != nil {
		t.Fatal(err)
	}
	if err = s.Deactivate(b.SessionID); err != nil {
		t.Fatal(err)
	}
	_, err = s.Pool.Exec(context.Background(), `UPDATE auth_bridge.bindings SET revoked_at=now()-interval '2 days' WHERE session_id=$1`, b.SessionID)
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.Pool.Exec(context.Background(), `UPDATE auth_bridge.launches SET retired_at=now()-interval '2 days' WHERE caller=$1`, user)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.Purge(context.Background(), time.Now().Add(-24*time.Hour), time.Now().Add(-7*24*time.Hour)); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Binding(b.SessionID); err == nil {
		t.Fatal("expired inactive row was retained")
	}
	l, err = s.Reserve(user, r, "uid")
	if err != nil || l.Binding == nil || l.Binding.SessionID != b.SessionID {
		t.Fatal("request tombstone removed too soon")
	}
}

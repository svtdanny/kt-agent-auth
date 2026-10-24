package legacy

import (
	"context"
	"encoding/json"
	"example.com/auth-demo/internal/api"
	"example.com/auth-demo/internal/store"
	"github.com/google/uuid"
	bolt "go.etcd.io/bbolt"
	"os"
	"path/filepath"
	"testing"
)

func TestLegacyImportPreservesRevocationAndNeverResurrectsPurgedRows(t *testing.T) {
	url := os.Getenv("AUTH_DEMO_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("run scripts/test_postgres.py")
	}
	s, err := store.Open(url)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := context.Background()
	user := "legacy-test-" + uuid.NewString()
	id := uuid.NewString()
	t.Cleanup(func() {
		_, _ = s.Pool.Exec(ctx, `DELETE FROM auth_bridge.launches WHERE caller=$1`, user)
		_, _ = s.Pool.Exec(ctx, `DELETE FROM auth_bridge.bindings WHERE session_id=$1`, id)
		_, _ = s.Pool.Exec(ctx, `DELETE FROM auth_bridge.metadata WHERE key='legacy_import'`)
	})
	request := api.CreateSession{Agent: api.Ref{Namespace: "demo", Name: "mastra-http"}, ServiceAccount: "reader", RequestID: "legacy"}
	b := api.Binding{SessionID: id, Atespace: "demo", User: user, Agent: request.Agent, Account: api.Ref{Namespace: "demo", Name: "reader"}, AccountUID: "uid", Active: true}
	l := store.Launch{User: user, Request: request, AccountUID: "uid", Binding: &b}
	launch, _ := json.Marshal(l)
	b.Active = false
	binding, _ := json.Marshal(b)
	path := filepath.Join(t.TempDir(), "bindings.db")
	old, err := bolt.Open(path, 0600, nil)
	if err != nil {
		t.Fatal(err)
	}
	err = old.Update(func(tx *bolt.Tx) error {
		bucket, err := tx.CreateBucket([]byte("auth-demo"))
		if err != nil {
			return err
		}
		if err = bucket.Put([]byte("session:"+id), binding); err != nil {
			return err
		}
		return bucket.Put([]byte("launch:test"), launch)
	})
	old.Close()
	if err != nil {
		t.Fatal(err)
	}
	if _, err = Import(ctx, s, path); err != nil {
		t.Fatal(err)
	}
	got, err := s.Binding(id)
	if err != nil || got.Active {
		t.Fatal("revoked legacy binding became active")
	}
	saved, err := s.Reserve(user, request, "uid")
	if err != nil || saved.Binding == nil || saved.Binding.SessionID != id {
		t.Fatal("legacy idempotency mapping lost")
	}
	_, _ = s.Pool.Exec(ctx, `DELETE FROM auth_bridge.bindings WHERE session_id=$1`, id)
	if _, err = Import(ctx, s, path); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Binding(id); err == nil {
		t.Fatal("re-import resurrected a purged binding")
	}
}

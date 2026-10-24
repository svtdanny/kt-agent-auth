// Package legacy is used only by the one-time bbolt migration command.
package legacy

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"example.com/auth-demo/internal/api"
	"example.com/auth-demo/internal/store"
	bolt "go.etcd.io/bbolt"
)

func Import(ctx context.Context, s *store.Store, path string) (map[string]int, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(713627895)`); err != nil {
		return nil, err
	}
	var done bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM auth_bridge.metadata WHERE key='legacy_import')`).Scan(&done); err != nil {
		return nil, err
	}
	if done {
		return map[string]int{"alreadyImported": 1}, nil
	}
	old, err := bolt.Open(path, 0600, &bolt.Options{ReadOnly: true, Timeout: 5 * time.Second})
	if err != nil {
		return nil, err
	}
	defer old.Close()
	bindings := map[string]api.Binding{}
	launches := []store.Launch{}
	err = old.View(func(t *bolt.Tx) error {
		bucket := t.Bucket([]byte("auth-demo"))
		if bucket == nil {
			return fmt.Errorf("legacy bucket missing")
		}
		return bucket.ForEach(func(k, v []byte) error {
			switch {
			case strings.HasPrefix(string(k), "session:"):
				var b api.Binding
				if err := json.Unmarshal(v, &b); err != nil {
					return err
				}
				bindings[b.SessionID] = b
			case strings.HasPrefix(string(k), "launch:"):
				var l store.Launch
				if err := json.Unmarshal(v, &l); err != nil {
					return err
				}
				launches = append(launches, l)
			}
			return nil
		})
	})
	if err != nil {
		return nil, err
	}
	for _, b := range bindings {
		raw, _ := json.Marshal(b)
		_, err = tx.Exec(ctx, `INSERT INTO auth_bridge.bindings(session_id,data,active,revoked_at) VALUES($1,$2,$3,CASE WHEN $3 THEN NULL ELSE now() END) ON CONFLICT DO NOTHING`, b.SessionID, raw, b.Active)
		if err != nil {
			return nil, err
		}
	}
	for _, l := range launches {
		request, _ := json.Marshal(l.Request)
		var binding []byte
		retired := false
		if l.Binding != nil {
			binding, _ = json.Marshal(l.Binding)
			if b, ok := bindings[l.Binding.SessionID]; ok {
				retired = !b.Active
			}
		}
		_, err = tx.Exec(ctx, `INSERT INTO auth_bridge.launches(caller,request_id,request,account_uid,binding,retired_at)
   VALUES($1,$2,$3,$4,$5,CASE WHEN $6 THEN now() ELSE NULL END) ON CONFLICT DO NOTHING`, l.User, l.Request.RequestID, request, l.AccountUID, binding, retired)
		if err != nil {
			return nil, err
		}
	}
	counts := map[string]int{"bindings": len(bindings), "launches": len(launches)}
	raw, _ := json.Marshal(counts)
	_, err = tx.Exec(ctx, `INSERT INTO auth_bridge.metadata(key,value) VALUES('legacy_import',$1)`, string(raw))
	if err != nil {
		return nil, err
	}
	return counts, tx.Commit(ctx)
}

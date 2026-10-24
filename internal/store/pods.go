package store

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"example.com/auth-demo/internal/api"
)

func (s *Store) RegisterPod(b api.Binding) error {
	if b.PodUID == "" || b.PodName == "" || b.SessionID != "" || b.AccountUID == "" || !b.Active {
		return fmt.Errorf("invalid Pod binding")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	raw, _ := json.Marshal(b)
	_, err := s.Pool.Exec(ctx, `INSERT INTO auth_bridge.pod_bindings(pod_uid,data) VALUES($1,$2) ON CONFLICT DO NOTHING`, b.PodUID, raw)
	if err != nil {
		return err
	}
	old, err := s.PodBinding(b.PodUID)
	if err != nil {
		return err
	}
	if old != b {
		return fmt.Errorf("Pod identity binding is immutable")
	}
	return nil
}

func (s *Store) PodBinding(uid string) (api.Binding, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var raw []byte
	if err := s.Pool.QueryRow(ctx, `SELECT data FROM auth_bridge.pod_bindings WHERE pod_uid=$1`, uid).Scan(&raw); err != nil {
		return api.Binding{}, fmt.Errorf("unregistered Pod identity")
	}
	var b api.Binding
	err := json.Unmarshal(raw, &b)
	return b, err
}

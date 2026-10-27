// Package store owns only the auth_bridge PostgreSQL schema.
package store

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"example.com/auth-demo/internal/api"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Repository interface {
	Reserve(string, api.CreateSession, string) (Launch, error)
	Activate(Launch, api.Binding) error
	Binding(string) (api.Binding, error)
	Deactivate(string) error
	ActiveBefore(context.Context, time.Time) ([]api.Binding, error)
	Purge(context.Context, time.Time, time.Time) (int64, error)
}
type Store struct{ Pool *pgxpool.Pool }
type Launch struct {
	User       string            `json:"user"`
	Request    api.CreateSession `json:"request"`
	AccountUID string            `json:"accountUid"`
	Binding    *api.Binding      `json:"binding,omitempty"`
}

func Open(url string) (*Store, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	config, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	config.MaxConns = 4
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, err
	}
	s := &Store{Pool: pool}
	if err = pool.Ping(ctx); err == nil {
		err = s.migrate(ctx)
	}
	if err != nil {
		pool.Close()
		return nil, err
	}
	return s, nil
}
func (s *Store) Close() error { s.Pool.Close(); return nil }
func (s *Store) migrate(ctx context.Context) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(713627894);
CREATE TABLE IF NOT EXISTS auth_bridge.bindings (
 session_id text PRIMARY KEY,data jsonb NOT NULL,active boolean NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),revoked_at timestamptz);
CREATE INDEX IF NOT EXISTS bindings_gc ON auth_bridge.bindings(active,created_at);
CREATE TABLE IF NOT EXISTS auth_bridge.launches (
 caller text NOT NULL,request_id text NOT NULL,request jsonb NOT NULL,
 account_uid text NOT NULL,binding jsonb,created_at timestamptz NOT NULL DEFAULT now(),
 retired_at timestamptz,PRIMARY KEY(caller,request_id));
`)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func (s *Store) Reserve(user string, r api.CreateSession, uid string) (Launch, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	raw, _ := json.Marshal(r)
	_, err := s.Pool.Exec(ctx, `INSERT INTO auth_bridge.launches(caller,request_id,request,account_uid)
 VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, user, r.RequestID, raw, uid)
	if err != nil {
		return Launch{}, err
	}
	var saved, binding []byte
	var savedUID string
	err = s.Pool.QueryRow(ctx, `SELECT request,account_uid,binding FROM auth_bridge.launches WHERE caller=$1 AND request_id=$2`, user, r.RequestID).Scan(&saved, &savedUID, &binding)
	if err != nil {
		return Launch{}, err
	}
	l := Launch{User: user, AccountUID: savedUID}
	if err = json.Unmarshal(saved, &l.Request); err != nil {
		return l, err
	}
	if l.Request != r || savedUID != uid {
		return l, fmt.Errorf("requestId is already bound to another launch")
	}
	if len(binding) > 0 {
		l.Binding = &api.Binding{}
		err = json.Unmarshal(binding, l.Binding)
	}
	return l, err
}
func (s *Store) Activate(l Launch, b api.Binding) error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var existing []byte
	err = tx.QueryRow(ctx, `SELECT binding FROM auth_bridge.launches WHERE caller=$1 AND request_id=$2 FOR UPDATE`, l.User, l.Request.RequestID).Scan(&existing)
	if err != nil {
		return err
	}
	if len(existing) > 0 {
		var old api.Binding
		if err = json.Unmarshal(existing, &old); err != nil {
			return err
		}
		if old != b {
			return fmt.Errorf("session binding is immutable")
		}
		return tx.Commit(ctx)
	}
	if b.User != l.User || b.AccountUID != l.AccountUID || b.Agent != l.Request.Agent || b.Account.Name != l.Request.ServiceAccount {
		return fmt.Errorf("binding does not match reserved launch")
	}
	raw, _ := json.Marshal(b)
	_, err = tx.Exec(ctx, `INSERT INTO auth_bridge.bindings(session_id,data,active) VALUES($1,$2,$3)`, b.SessionID, raw, b.Active)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE auth_bridge.launches SET binding=$3 WHERE caller=$1 AND request_id=$2`, l.User, l.Request.RequestID, raw)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func (s *Store) Binding(id string) (api.Binding, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var raw []byte
	var active bool
	err := s.Pool.QueryRow(ctx, `SELECT data,active FROM auth_bridge.bindings WHERE session_id=$1`, id).Scan(&raw, &active)
	if err == pgx.ErrNoRows {
		return api.Binding{}, fmt.Errorf("unregistered session")
	}
	if err != nil {
		return api.Binding{}, err
	}
	var b api.Binding
	err = json.Unmarshal(raw, &b)
	b.Active = active
	return b, err
}
func (s *Store) Deactivate(id string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, `UPDATE auth_bridge.bindings SET active=false,revoked_at=COALESCE(revoked_at,now()) WHERE session_id=$1`, id)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE auth_bridge.launches SET retired_at=COALESCE(retired_at,now()) WHERE binding->>'sessionId'=$1`, id)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func (s *Store) ActiveBefore(ctx context.Context, before time.Time) ([]api.Binding, error) {
	rows, err := s.Pool.Query(ctx, `SELECT data FROM auth_bridge.bindings WHERE active AND created_at<$1 ORDER BY session_id`, before)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []api.Binding{}
	for rows.Next() {
		var raw []byte
		if err = rows.Scan(&raw); err != nil {
			return nil, err
		}
		var b api.Binding
		if err = json.Unmarshal(raw, &b); err != nil {
			return nil, err
		}
		b.Active = true
		result = append(result, b)
	}
	return result, rows.Err()
}
func (s *Store) Purge(ctx context.Context, bindingBefore, requestBefore time.Time) (int64, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, `DELETE FROM auth_bridge.bindings WHERE NOT active AND revoked_at<$1`, bindingBefore)
	if err != nil {
		return 0, err
	}
	_, err = tx.Exec(ctx, `DELETE FROM auth_bridge.launches WHERE retired_at<$1`, requestBefore)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), tx.Commit(ctx)
}

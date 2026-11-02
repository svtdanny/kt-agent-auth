-- Reference DDL for one project database, also used by the no-patch design.
-- Source: internal/store/store.go and pkg/authstate/cache.go.
-- Database and LOGIN are provisioned per project by scripts/deploy.py.
-- JSON references are not SQL foreign keys.

CREATE SCHEMA IF NOT EXISTS auth_bridge;
REVOKE ALL ON SCHEMA auth_bridge FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS auth_state;
REVOKE ALL ON SCHEMA auth_state FROM PUBLIC;

CREATE TABLE IF NOT EXISTS auth_bridge.bindings (
 session_id text PRIMARY KEY,data jsonb NOT NULL,active boolean NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),revoked_at timestamptz);
CREATE INDEX IF NOT EXISTS bindings_gc ON auth_bridge.bindings(active,created_at);
CREATE TABLE IF NOT EXISTS auth_bridge.launches (
 caller text NOT NULL,request_id text NOT NULL,request jsonb NOT NULL,
 account_uid text NOT NULL,binding jsonb,created_at timestamptz NOT NULL DEFAULT now(),
 retired_at timestamptz,PRIMARY KEY(caller,request_id));

CREATE TABLE IF NOT EXISTS auth_state.tokens (
 id text PRIMARY KEY, version bigint NOT NULL DEFAULT 0,
 ciphertext bytea, expires_at timestamptz NOT NULL DEFAULT '-infinity',
 owner text NOT NULL DEFAULT '', lease_until timestamptz NOT NULL DEFAULT '-infinity',
 retry_after timestamptz NOT NULL DEFAULT '-infinity');

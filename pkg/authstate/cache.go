// Package authstate shares encrypted, expiring credentials between replicas.
// Callers supply the authentication protocol; this package owns persistence,
// encryption, refresh leases and a small, bounded-staleness memory cache.
package authstate

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sync/singleflight"
)

const (
	recheck  = time.Second
	lease    = 12 * time.Second
	cooldown = 2 * time.Second
)

// Token.Expires is the safe reuse deadline, including the caller's refresh margin.
type Token struct {
	Value   string    `json:"value"`
	Expires time.Time `json:"expires"`
}

type cached struct {
	token Token
	check time.Time
}

type Cache struct {
	pool  *pgxpool.Pool
	aead  cipher.AEAD
	key   []byte
	mu    sync.Mutex
	local map[string]cached
	loads singleflight.Group
}

// New uses a project-scoped pool and a 32-byte key mounted from a Secret.
// The key must be identical on all replicas and is never stored in PostgreSQL.
func New(ctx context.Context, pool *pgxpool.Pool, key []byte) (*Cache, error) {
	if len(key) != 32 {
		return nil, fmt.Errorf("auth-state key must contain 32 bytes")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(713627895);
CREATE SCHEMA IF NOT EXISTS auth_state;
REVOKE ALL ON SCHEMA auth_state FROM PUBLIC;
CREATE TABLE IF NOT EXISTS auth_state.tokens (
 id text PRIMARY KEY, version bigint NOT NULL DEFAULT 0,
 ciphertext bytea, expires_at timestamptz NOT NULL DEFAULT '-infinity',
 owner text NOT NULL DEFAULT '', lease_until timestamptz NOT NULL DEFAULT '-infinity',
 retry_after timestamptz NOT NULL DEFAULT '-infinity');`)
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &Cache{pool: pool, aead: aead, key: append([]byte(nil), key...), local: map[string]cached{}}, nil
}

// Key creates an opaque, stable identity from scope and credential configuration.
// Include project, credential reference and its complete configuration/revision.
func (c *Cache) Key(parts ...string) string {
	raw, _ := json.Marshal(parts)
	h := hmac.New(sha256.New, c.key)
	h.Write(raw)
	return hex.EncodeToString(h.Sum(nil))
}

func (c *Cache) seal(id string, version int64, token Token) ([]byte, error) {
	raw, err := json.Marshal(token)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, c.aead.NonceSize())
	if _, err = rand.Read(nonce); err != nil {
		return nil, err
	}
	return c.aead.Seal(nonce, nonce, raw, []byte(fmt.Sprintf("%s/%d", id, version))), nil
}

func (c *Cache) open(id string, version int64, data []byte) (Token, error) {
	var token Token
	n := c.aead.NonceSize()
	if len(data) < n {
		return token, errors.New("invalid encrypted auth state")
	}
	raw, err := c.aead.Open(nil, data[:n], data[n:], []byte(fmt.Sprintf("%s/%d", id, version)))
	if err != nil {
		return token, errors.New("invalid encrypted auth state")
	}
	if err = json.Unmarshal(raw, &token); err != nil || token.Value == "" {
		return Token{}, errors.New("invalid encrypted auth state")
	}
	return token, nil
}

// Get coalesces local requests and coordinates other replicas using a short SQL
// lease. No transaction or database connection is held during acquire.
func (c *Cache) Get(ctx context.Context, id string, acquire func(context.Context) (Token, error)) (Token, error) {
	c.mu.Lock()
	v, ok := c.local[id]
	c.mu.Unlock()
	if ok && time.Now().Before(v.check) && time.Now().Before(v.token.Expires) {
		return v.token, nil
	}
	result := c.loads.DoChan(id, func() (any, error) {
		work, cancel := context.WithTimeout(context.WithoutCancel(ctx), 20*time.Second)
		defer cancel()
		token, err := c.get(work, id, acquire)
		if err == nil {
			c.mu.Lock()
			c.local[id] = cached{token: token, check: time.Now().Add(recheck)}
			c.mu.Unlock()
		}
		return token, err
	})
	select {
	case <-ctx.Done():
		return Token{}, ctx.Err()
	case r := <-result:
		if r.Err != nil {
			return Token{}, r.Err
		}
		return r.Val.(Token), nil
	}
}

func (c *Cache) get(ctx context.Context, id string, acquire func(context.Context) (Token, error)) (Token, error) {
	for {
		var version int64
		var data []byte
		var valid, waiting bool
		err := c.pool.QueryRow(ctx, `SELECT version,ciphertext,expires_at>clock_timestamp(),retry_after>clock_timestamp()
FROM auth_state.tokens WHERE id=$1`, id).Scan(&version, &data, &valid, &waiting)
		if errors.Is(err, pgx.ErrNoRows) {
			if _, err = c.pool.Exec(ctx, `INSERT INTO auth_state.tokens(id) VALUES($1) ON CONFLICT DO NOTHING`, id); err != nil {
				return Token{}, err
			}
			continue
		}
		if err != nil {
			return Token{}, err
		}
		if len(data) > 0 && valid {
			token, err := c.open(id, version, data)
			if err != nil {
				return Token{}, err
			}
			if !time.Now().Before(token.Expires) {
				return Token{}, errors.New("auth-state deadline mismatch")
			}
			return token, nil
		}
		if waiting {
			return Token{}, errors.New("authentication refresh cooling down")
		}
		owner := uuid.NewString()
		err = c.pool.QueryRow(ctx, `UPDATE auth_state.tokens SET owner=$2,version=version+1,
lease_until=clock_timestamp()+$3::interval
WHERE id=$1 AND expires_at<=clock_timestamp() AND lease_until<=clock_timestamp()
AND retry_after<=clock_timestamp() RETURNING version`, id, owner, lease.String()).Scan(&version)
		if errors.Is(err, pgx.ErrNoRows) {
			timer := time.NewTimer(100 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return Token{}, ctx.Err()
			case <-timer.C:
				continue
			}
		}
		if err != nil {
			return Token{}, err
		}
		fetch, cancel := context.WithTimeout(ctx, 8*time.Second)
		token, err := acquire(fetch)
		cancel()
		if err == nil && (token.Value == "" || !time.Now().Before(token.Expires)) {
			err = errors.New("invalid acquired auth state")
		}
		if err != nil {
			// A shared cooldown also protects the provider when callers repeatedly retry.
			cleanup, done := context.WithTimeout(context.WithoutCancel(ctx), time.Second)
			_, _ = c.pool.Exec(cleanup, `UPDATE auth_state.tokens SET owner='',lease_until='-infinity',
retry_after=clock_timestamp()+$4::interval WHERE id=$1 AND owner=$2 AND version=$3`, id, owner, version, cooldown.String())
			done()
			return Token{}, err
		}
		data, err = c.seal(id, version, token)
		if err != nil {
			return Token{}, err
		}
		tag, err := c.pool.Exec(ctx, `UPDATE auth_state.tokens SET ciphertext=$4,expires_at=$5,
owner='',lease_until='-infinity',retry_after='-infinity'
WHERE id=$1 AND owner=$2 AND version=$3 AND lease_until>clock_timestamp()`, id, owner, version, data, token.Expires)
		if err != nil {
			return Token{}, err
		}
		if tag.RowsAffected() != 1 {
			return Token{}, errors.New("authentication refresh superseded")
		}
		return token, nil
	}
}

// Invalidate is an explicit administrative action, not an unversioned response
// to an upstream 401. Other replicas observe it within the recheck interval.
func (c *Cache) Invalidate(ctx context.Context, id string) error {
	_, err := c.pool.Exec(ctx, `UPDATE auth_state.tokens SET version=version+1,ciphertext=NULL,
expires_at='-infinity',owner='',lease_until='-infinity',retry_after='-infinity' WHERE id=$1`, id)
	c.mu.Lock()
	delete(c.local, id)
	c.mu.Unlock()
	return err
}

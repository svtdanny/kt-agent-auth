package authstate

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"errors"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestEncryptionBindsRecordAndVersion(t *testing.T) {
	block, _ := aes.NewCipher(bytes.Repeat([]byte{1}, 32))
	aead, _ := cipher.NewGCM(block)
	c := &Cache{aead: aead}
	token := Token{Value: "never-plaintext", Expires: time.Now().Add(time.Minute)}
	data, err := c.seal("project-a", 3, token)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(data, []byte(token.Value)) {
		t.Fatal("plaintext stored")
	}
	if got, err := c.open("project-a", 3, data); err != nil || got.Value != token.Value {
		t.Fatal(got, err)
	}
	for _, tc := range []struct {
		id      string
		version int64
	}{{"project-b", 3}, {"project-a", 4}} {
		if _, err := c.open(tc.id, tc.version, data); err == nil {
			t.Fatal("ciphertext transplant accepted")
		}
	}
	tampered := append([]byte(nil), data...)
	tampered[len(tampered)-1] ^= 1
	if _, err := c.open("project-a", 3, tampered); err == nil {
		t.Fatal("tampering accepted")
	}
	other, _ := aes.NewCipher(bytes.Repeat([]byte{2}, 32))
	c.aead, _ = cipher.NewGCM(other)
	if _, err := c.open("project-a", 3, data); err == nil {
		t.Fatal("wrong key accepted")
	}
}

func testCache(t *testing.T) (*Cache, *pgxpool.Pool) {
	t.Helper()
	url := os.Getenv("AUTH_DEMO_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("requires isolated PostgreSQL integration database")
	}
	pool, err := pgxpool.New(context.Background(), url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	c, err := New(context.Background(), pool, bytes.Repeat([]byte{1}, 32))
	if err != nil {
		t.Fatal(err)
	}
	return c, pool
}

func TestPostgresReplicasRestartRefreshAndCooldown(t *testing.T) {
	a, pool := testCache(t)
	ctx := context.Background()
	b, err := New(ctx, pool, bytes.Repeat([]byte{1}, 32))
	if err != nil {
		t.Fatal(err)
	}
	id := a.Key("project-a", uuid.NewString())
	t.Cleanup(func() { pool.Exec(ctx, "DELETE FROM auth_state.tokens WHERE id=$1", id) })
	var calls atomic.Int64
	fetch := func(context.Context) (Token, error) {
		n := calls.Add(1)
		time.Sleep(25 * time.Millisecond)
		return Token{Value: "secret-" + string(rune('0'+n)), Expires: time.Now().Add(300 * time.Millisecond)}, nil
	}
	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			c := []*Cache{a, b}[i%2]
			v, e := c.Get(ctx, id, fetch)
			if e != nil || v.Value != "secret-1" {
				t.Errorf("acquire: %v %v", v, e)
			}
		}(i)
	}
	wg.Wait()
	if calls.Load() != 1 {
		t.Fatal("replica stampede", calls.Load())
	}
	var encrypted []byte
	if err := pool.QueryRow(ctx, "SELECT ciphertext FROM auth_state.tokens WHERE id=$1", id).Scan(&encrypted); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(encrypted, []byte("secret-1")) {
		t.Fatal("plaintext in PostgreSQL")
	}
	restarted, err := New(ctx, pool, bytes.Repeat([]byte{1}, 32))
	if err != nil {
		t.Fatal(err)
	}
	if v, e := restarted.Get(ctx, id, fetch); e != nil || v.Value != "secret-1" || calls.Load() != 1 {
		t.Fatal("restart did not reuse shared state", e, calls.Load())
	}
	time.Sleep(350 * time.Millisecond)
	if v, e := b.Get(ctx, id, fetch); e != nil || v.Value != "secret-2" || calls.Load() != 2 {
		t.Fatal("expiry did not refresh", e, calls.Load())
	}
	if err := a.Invalidate(ctx, id); err != nil {
		t.Fatal(err)
	}
	if v, e := a.Get(ctx, id, fetch); e != nil || v.Value != "secret-3" {
		t.Fatal("invalidation failed", e)
	}
	if err := a.Invalidate(ctx, id); err != nil {
		t.Fatal(err)
	}
	var failures atomic.Int64
	fail := func(context.Context) (Token, error) { failures.Add(1); return Token{}, errors.New("provider down") }
	for _, c := range []*Cache{a, restarted} {
		if _, e := c.Get(ctx, id, fail); e == nil {
			t.Fatal("provider error ignored")
		}
	}
	if failures.Load() != 1 {
		t.Fatal("shared cooldown missing", failures.Load())
	}
}

func TestPostgresSupersededLeaseCannotPublish(t *testing.T) {
	c, pool := testCache(t)
	ctx := context.Background()
	id := c.Key(uuid.NewString())
	t.Cleanup(func() { pool.Exec(ctx, "DELETE FROM auth_state.tokens WHERE id=$1", id) })
	started, release := make(chan struct{}), make(chan struct{})
	done := make(chan error, 1)
	go func() {
		_, e := c.Get(ctx, id, func(context.Context) (Token, error) {
			close(started)
			<-release
			return Token{Value: "old", Expires: time.Now().Add(time.Minute)}, nil
		})
		done <- e
	}()
	<-started
	if err := c.Invalidate(ctx, id); err != nil {
		t.Fatal(err)
	}
	close(release)
	if err := <-done; err == nil {
		t.Fatal("late refresh published after invalidation")
	}
	// Simulate a crashed owner whose lease has elapsed, without waiting 12 seconds.
	_, err := pool.Exec(ctx, "UPDATE auth_state.tokens SET owner='crashed',lease_until=clock_timestamp()-interval '1 second' WHERE id=$1", id)
	if err != nil {
		t.Fatal(err)
	}
	if v, e := c.Get(ctx, id, func(context.Context) (Token, error) {
		return Token{Value: "recovered", Expires: time.Now().Add(time.Minute)}, nil
	}); e != nil || v.Value != "recovered" {
		t.Fatal(v, e)
	}
}

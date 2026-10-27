package controlplane

import (
	"context"
	"example.com/auth-demo/internal/api"
	"example.com/auth-demo/internal/store"
	"fmt"
	"sync"
	"time"
)

// Test double only; production uses PostgreSQL exclusively.
type memoryStore struct {
	mu       sync.Mutex
	launches map[string]store.Launch
	bindings map[string]api.Binding
	created  map[string]time.Time
	purges   int
}

func newMemoryStore() *memoryStore {
	return &memoryStore{launches: map[string]store.Launch{}, bindings: map[string]api.Binding{}, created: map[string]time.Time{}}
}
func (m *memoryStore) Reserve(user string, r api.CreateSession, uid string) (store.Launch, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	key := user + "\x00" + r.RequestID
	if l, ok := m.launches[key]; ok {
		if l.Request != r || l.AccountUID != uid {
			return l, fmt.Errorf("immutable launch")
		}
		return l, nil
	}
	l := store.Launch{User: user, Request: r, AccountUID: uid}
	m.launches[key] = l
	return l, nil
}
func (m *memoryStore) Activate(l store.Launch, b api.Binding) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	key := l.User + "\x00" + l.Request.RequestID
	if old := m.launches[key]; old.Binding != nil && *old.Binding != b {
		return fmt.Errorf("immutable binding")
	}
	l.Binding = &b
	m.launches[key] = l
	m.bindings[b.SessionID] = b
	m.created[b.SessionID] = time.Now()
	return nil
}
func (m *memoryStore) Binding(id string) (api.Binding, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	b, ok := m.bindings[id]
	if !ok {
		return b, fmt.Errorf("unregistered session")
	}
	return b, nil
}
func (m *memoryStore) Deactivate(id string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	b := m.bindings[id]
	b.Active = false
	m.bindings[id] = b
	return nil
}
func (m *memoryStore) ActiveBefore(_ context.Context, before time.Time) ([]api.Binding, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := []api.Binding{}
	for id, b := range m.bindings {
		if b.Active && m.created[id].Before(before) {
			out = append(out, b)
		}
	}
	return out, nil
}
func (m *memoryStore) Purge(context.Context, time.Time, time.Time) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.purges++
	return 0, nil
}

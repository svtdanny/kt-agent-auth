package controlplane

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	apiv1 "github.com/kagent-dev/kagent/go/api/gen/kagent/api/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type GCResult struct {
	Listed, ConfirmedDeleted int
	Purged                   int64
}

func (s *Server) RunCleanup(ctx context.Context) {
	ticker := time.NewTicker(s.GCInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			run, cancel := context.WithTimeout(ctx, time.Minute)
			result, err := s.Reconcile(run)
			cancel()
			if err != nil {
				slog.Warn("session cleanup skipped", "error", err)
			} else {
				slog.Info("session cleanup complete", "listed", result.Listed, "revoked", result.ConfirmedDeleted, "purged", result.Purged)
			}
		}
	}
}

// Reconcile first finishes every page. Missing rows are confirmed with GetSession
// before any revocation. Suspended/idle sessions remain in ListSessions and are
// retained. Failed or partial scans never authorize destructive cleanup.
func (s *Server) Reconcile(ctx context.Context) (GCResult, error) {
	s.gcMu.Lock()
	defer s.gcMu.Unlock()
	started := time.Now()
	result := GCResult{}
	up, err := s.upstream(ctx)
	if err != nil {
		return result, err
	}
	live := map[string]bool{}
	tokens := map[string]bool{}
	token := ""
	for {
		page, err := s.Kagent.ListSessions(up, &apiv1.ListSessionsRequest{Page: &apiv1.PageRequest{Limit: 100, PageToken: token}})
		if err != nil {
			return result, err
		}
		for _, session := range page.Sessions {
			live[session.Id] = true
		}
		token = page.GetPage().GetNextPageToken()
		if token == "" {
			break
		}
		if tokens[token] {
			return result, fmt.Errorf("ListSessions repeated its pagination token")
		}
		tokens[token] = true
	}
	result.Listed = len(live)
	bindings, err := s.Store.ActiveBefore(ctx, started.Add(-time.Minute))
	if err != nil {
		return result, err
	}
	missing := []string{}
	for _, b := range bindings {
		if live[b.SessionID] {
			continue
		}
		s.NativeReads.Add(1)
		_, err := s.Kagent.GetSession(up, &apiv1.GetSessionRequest{SessionId: b.SessionID})
		if status.Code(err) == codes.NotFound {
			missing = append(missing, b.SessionID)
		} else if err != nil {
			return result, err
		}
	}
	for _, id := range missing {
		if err = s.Store.Deactivate(id); err != nil {
			return result, err
		}
		result.ConfirmedDeleted++
	}
	result.Purged, err = s.Store.Purge(ctx, started.Add(-s.BindingRetention), started.Add(-s.RequestRetention))
	return result, err
}

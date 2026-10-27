package controlplane

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"example.com/auth-demo/internal/api"
	"example.com/auth-demo/internal/credential"
	"example.com/auth-demo/internal/policy"
	"example.com/auth-demo/internal/store"
	a2apb "github.com/a2aproject/a2a-go/v2/a2apb/v1"
	"github.com/google/uuid"
	apiv1 "github.com/kagent-dev/kagent/go/api/gen/kagent/api/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	authnv1 "k8s.io/api/authentication/v1"
	authzv1 "k8s.io/api/authorization/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

type Server struct {
	TokenReviews     atomic.Uint64
	SelectionReviews atomic.Uint64
	VersionConflicts atomic.Uint64
	Kube             kubernetes.Interface
	Store            store.Repository
	Kagent           apiv1.SessionServiceClient
	A2A              a2apb.A2AServiceClient
	Namespace        string
	AgentName        string
	TokenPath        string
	Objects          ObjectState
	Credentials      credential.Provider
	gcMu             sync.Mutex
	ContextReads     atomic.Uint64
	GrantReads       atomic.Uint64
	NativeReads      atomic.Uint64
	GCInterval       time.Duration
	BindingRetention time.Duration
	RequestRetention time.Duration
}

func New(k kubernetes.Interface, s store.Repository, target, ns, token string, objects ObjectState, credentials credential.Provider) (*Server, error) {
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	server := &Server{Kube: k, Store: s, Kagent: apiv1.NewSessionServiceClient(conn), A2A: a2apb.NewA2AServiceClient(conn), Namespace: ns, TokenPath: token, Objects: objects, Credentials: credentials,
		GCInterval: 5 * time.Minute, BindingRetention: 24 * time.Hour, RequestRetention: 7 * 24 * time.Hour}
	return server, nil
}

// Kagent sees the bridge's platform identity. End-user ownership is enforced by this bridge.
func (s *Server) upstream(ctx context.Context) (context.Context, error) {
	token, err := os.ReadFile(s.TokenPath)
	if err != nil {
		return nil, err
	}
	return metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer "+strings.TrimSpace(string(token)))), nil
}
func write(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func reject(w http.ResponseWriter, status int, err error) {
	write(w, status, map[string]string{"error": err.Error()})
}
func decode(w http.ResponseWriter, r *http.Request, out any) error {
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	d.DisallowUnknownFields()
	return d.Decode(out)
}

func (s *Server) Handler() http.Handler {
	m := http.NewServeMux()
	m.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) { write(w, 200, map[string]bool{"ready": true}) })
	m.HandleFunc("POST /v1/sessions", s.create)
	m.HandleFunc("POST /v1/sessions/{id}/invoke", s.invoke)
	m.HandleFunc("DELETE /v1/sessions/{id}", s.delete)
	m.HandleFunc("GET /internal/stats", func(w http.ResponseWriter, r *http.Request) {
		if !internal(r) {
			reject(w, 403, fmt.Errorf("ext-proc client certificate required"))
			return
		}
		stats := s.Objects.Stats()
		for key, value := range s.Credentials.Stats() {
			stats[key] = value
		}
		stats["tokenReviews"] = s.TokenReviews.Load()
		stats["selectionReviews"] = s.SelectionReviews.Load()
		stats["policyVersionConflicts"] = s.VersionConflicts.Load()
		stats["contextRequests"] = s.ContextReads.Load()
		stats["grantRequests"] = s.GrantReads.Load()
		stats["nativeSessionGETs"] = s.NativeReads.Load()
		write(w, 200, stats)
	})
	m.HandleFunc("POST /internal/reconcile", func(w http.ResponseWriter, r *http.Request) {
		if !internal(r) {
			reject(w, 403, fmt.Errorf("ext-proc client certificate required"))
			return
		}
		result, err := s.Reconcile(r.Context())
		if err != nil {
			reject(w, 503, err)
			return
		}
		write(w, 200, result)
	})
	m.HandleFunc("POST /internal/credentials/invalidate", func(w http.ResponseWriter, r *http.Request) {
		if !internal(r) {
			reject(w, 403, fmt.Errorf("ext-proc client certificate required"))
			return
		}
		var input struct {
			Namespace string `json:"namespace"`
			Name      string `json:"name"`
		}
		if err := decode(w, r, &input); err != nil {
			reject(w, 400, err)
			return
		}
		if err := s.Credentials.Invalidate(input.Namespace, input.Name); err != nil {
			reject(w, 400, err)
			return
		}
		write(w, 200, map[string]bool{"invalidated": true})
	})
	return m
}
func internal(r *http.Request) bool {
	return r.TLS != nil && len(r.TLS.VerifiedChains) > 0 && len(r.TLS.PeerCertificates) > 0 && r.TLS.PeerCertificates[0].Subject.CommonName == "ext-proc"
}

func (s *Server) authenticate(r *http.Request) (authnv1.UserInfo, error) {
	s.TokenReviews.Add(1)
	h := r.Header.Get("Authorization")
	if !strings.HasPrefix(h, "Bearer ") {
		return authnv1.UserInfo{}, fmt.Errorf("bearer token required")
	}
	result, err := s.Kube.AuthenticationV1().TokenReviews().Create(r.Context(), &authnv1.TokenReview{Spec: authnv1.TokenReviewSpec{Token: strings.TrimPrefix(h, "Bearer "), Audiences: []string{"auth-demo"}}}, metav1.CreateOptions{})
	if err != nil {
		return authnv1.UserInfo{}, err
	}
	if !result.Status.Authenticated || result.Status.User.Username == "" {
		return authnv1.UserInfo{}, fmt.Errorf("invalid caller token")
	}
	return result.Status.User, nil
}
func (s *Server) canUse(ctx context.Context, u authnv1.UserInfo, name string) error {
	s.SelectionReviews.Add(1)
	review, err := s.Kube.AuthorizationV1().SubjectAccessReviews().Create(ctx, &authzv1.SubjectAccessReview{Spec: authzv1.SubjectAccessReviewSpec{User: u.Username, UID: u.UID, Groups: u.Groups, ResourceAttributes: &authzv1.ResourceAttributes{Namespace: s.Namespace, Verb: "use", Resource: "serviceaccounts", Name: name}}}, metav1.CreateOptions{})
	if err != nil {
		return err
	}
	if !review.Status.Allowed {
		return fmt.Errorf("caller may not use service account %s", name)
	}
	return nil
}
func (s *Server) create(w http.ResponseWriter, r *http.Request) {
	u, err := s.authenticate(r)
	if err != nil {
		reject(w, 401, err)
		return
	}
	var in api.CreateSession
	if err = decode(w, r, &in); err != nil {
		reject(w, 400, err)
		return
	}
	if in.Agent.Namespace != s.Namespace || (s.AgentName != "" && in.Agent.Name != s.AgentName) || in.Agent.Name == "" || in.ServiceAccount == "" || in.RequestID == "" {
		reject(w, 400, fmt.Errorf("agent, account and requestId in the demo namespace are required"))
		return
	}
	if err = s.canUse(r.Context(), u, in.ServiceAccount); err != nil {
		reject(w, 403, err)
		return
	}
	sa, err := s.Objects.Account(r.Context(), s.Namespace, in.ServiceAccount)
	if err != nil {
		reject(w, 403, err)
		return
	}
	if _, policyErr := s.Objects.Policy(r.Context(), sa); policyErr != nil {
		reject(w, 403, fmt.Errorf("account has no policy type"))
		return
	}
	// Reservation and activation are protected by PostgreSQL uniqueness and
	// transactions, including when requests arrive at different bridge replicas.
	l, err := s.Store.Reserve(u.Username, in, string(sa.UID))
	if err != nil {
		reject(w, 409, err)
		return
	}
	if l.Binding != nil {
		b, e := s.Store.Binding(l.Binding.SessionID)
		if e != nil || !b.Active {
			reject(w, 409, fmt.Errorf("session is no longer active"))
			return
		}
		write(w, 200, b)
		return
	}
	ctx, err := s.upstream(r.Context())
	if err != nil {
		reject(w, 503, err)
		return
	}
	// Scope upstream idempotency by authenticated caller, never by a caller-supplied user header.
	key := fmt.Sprintf("auth-demo-%x", sha256.Sum256([]byte(s.Namespace+"/"+s.AgentName+"\x00"+u.Username+"\x00"+in.RequestID)))
	created, err := s.Kagent.CreateSession(ctx, &apiv1.CreateSessionRequest{Agent: &apiv1.ResourceReference{Namespace: in.Agent.Namespace, Name: in.Agent.Name}, RequestId: key})
	if err != nil {
		reject(w, 503, err)
		return
	}
	b := api.Binding{SessionID: created.Session.Id, Atespace: in.Agent.Namespace, User: u.Username, Agent: in.Agent, Account: api.Ref{Namespace: s.Namespace, Name: in.ServiceAccount}, AccountUID: string(sa.UID), Active: true}
	if err = s.Store.Activate(l, b); err != nil {
		reject(w, 503, err)
		return
	}
	slog.Info("session bound", "session", b.SessionID, "user", b.User, "serviceAccount", b.Account.String(), "policyRef", sa.Annotations[api.PolicyRefAnnotation])
	write(w, 200, b)
}
func (s *Server) owned(r *http.Request) (api.Binding, error) {
	u, err := s.authenticate(r)
	if err != nil {
		return api.Binding{}, err
	}
	b, err := s.Store.Binding(r.PathValue("id"))
	if err != nil {
		return b, err
	}
	if b.User != u.Username || b.Agent.Namespace != s.Namespace || (s.AgentName != "" && b.Agent.Name != s.AgentName) {
		return b, fmt.Errorf("session is not owned by caller")
	}
	return b, nil
}
func (s *Server) invoke(w http.ResponseWriter, r *http.Request) {
	b, err := s.owned(r)
	if err != nil {
		reject(w, 403, err)
		return
	}
	if !b.Active {
		reject(w, 403, fmt.Errorf("session binding is revoked"))
		return
	}
	var in api.InvokeRequest
	if err = decode(w, r, &in); err != nil {
		reject(w, 400, err)
		return
	}
	ctx, err := s.upstream(r.Context())
	if err != nil {
		reject(w, 503, err)
		return
	}
	raw, _ := json.Marshal(in)
	response, err := s.A2A.SendMessage(ctx, &a2apb.SendMessageRequest{Tenant: b.Agent.String(), Message: &a2apb.Message{MessageId: uuid.NewString(), ContextId: b.SessionID, Role: a2apb.Role_ROLE_USER, Parts: []*a2apb.Part{{Content: &a2apb.Part_Text{Text: string(raw)}}}}})
	if err != nil {
		reject(w, 502, err)
		return
	}
	out, err := protojson.Marshal(response)
	if err != nil {
		reject(w, 500, err)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(out)
}
func (s *Server) delete(w http.ResponseWriter, r *http.Request) {
	b, err := s.owned(r)
	if err != nil {
		reject(w, 403, err)
		return
	}
	if err = s.Store.Deactivate(b.SessionID); err != nil {
		reject(w, 503, err)
		return
	}
	ctx, err := s.upstream(r.Context())
	if err == nil {
		_, err = s.Kagent.DeleteSession(ctx, &apiv1.DeleteSessionRequest{SessionId: b.SessionID})
	}
	if status.Code(err) == codes.FailedPrecondition || status.Code(err) == codes.Aborted {
		reject(w, 409, err)
		return
	}
	if err != nil && status.Code(err) != codes.NotFound {
		reject(w, 503, err)
		return
	}
	write(w, 200, map[string]bool{"deleted": true})
}

func (s *Server) resolve(ctx context.Context, id string) (api.SessionContext, error) {
	s.ContextReads.Add(1)
	ns, sid, err := api.ParseActor(id)
	if err != nil {
		return api.SessionContext{}, err
	}
	b, err := s.Store.Binding(sid)
	if err != nil {
		return api.SessionContext{}, err
	}
	if !b.Active || b.Atespace != ns || b.Agent.Namespace != ns || ns != s.Namespace || (s.AgentName != "" && b.Agent.Name != s.AgentName) {
		return api.SessionContext{}, fmt.Errorf("inactive or mismatched session binding")
	}
	// Native Session identity was established at CreateSession. Egress uses
	// our immutable binding; the background reconciliation job detects native
	// deletion. No kagent or Kubernetes lookup is made for a warm cache hit.
	sa, err := s.Objects.Account(ctx, b.Account.Namespace, b.Account.Name)
	if err != nil {
		return api.SessionContext{}, err
	}
	if sa.Labels[api.ManagedLabel] != "true" || string(sa.UID) != b.AccountUID {
		return api.SessionContext{}, fmt.Errorf("service account was replaced")
	}
	p, err := s.Objects.Policy(ctx, sa)
	if err != nil {
		return api.SessionContext{}, err
	}
	return api.SessionContext{Binding: b, Policy: p}, nil
}

// Authorize is called in-process by ext_proc; no context/credential HTTP round trips.
func (s *Server) Authorize(ctx context.Context, id string, request api.Request) (api.Authorization, error) {
	for attempt := 0; attempt < 2; attempt++ {
		sc, err := s.resolve(ctx, id)
		if err != nil {
			return api.Authorization{}, &api.ControlError{Status: 403, Message: "caller is outside this agent or its binding is revoked"}
		}
		d, err := policy.Evaluate(ctx, sc, request)
		if err != nil {
			return api.Authorization{}, &api.ControlError{Status: 403, Message: "invalid request policy"}
		}
		result := api.Authorization{Context: sc, Decision: d}
		if !d.Allowed {
			return result, nil
		}
		if d.Credential != "" {
			value, err := s.Credentials.Read(ctx, sc.Binding.Account.Namespace, d.Credential)
			if err != nil {
				return api.Authorization{}, &api.ControlError{Status: 503, Message: "credential unavailable"}
			}
			if value == "" || strings.ContainsAny(value, "\r\n") {
				return api.Authorization{}, &api.ControlError{Status: 403, Message: "invalid credential"}
			}
			if d.Headers == nil {
				d.Headers = map[string]string{}
			}
			if prior, ok := d.Headers["authorization"]; ok && prior != "Bearer "+value {
				return api.Authorization{}, &api.ControlError{Status: 403, Message: "conflicting credentials"}
			}
			d.Headers["authorization"] = "Bearer " + value
		}
		// Credential acquisition can wait on a remote endpoint. Recheck revocation
		// after that wait as well as the watched policy snapshot.
		if d.Credential != "" {
			current, err := s.Store.Binding(sc.Binding.SessionID)
			if err != nil || !current.Active || current != sc.Binding {
				return api.Authorization{}, &api.ControlError{Status: 403, Message: "session binding revoked during authorization"}
			}
		}
		// No Kubernetes query is made here on cache hits.
		sa, err := s.Objects.Account(ctx, sc.Binding.Account.Namespace, sc.Binding.Account.Name)
		if err != nil || string(sa.UID) != sc.Binding.AccountUID {
			return api.Authorization{}, &api.ControlError{Status: 403, Message: "account identity revoked"}
		}
		latest, err := s.Objects.Policy(ctx, sa)
		if err != nil {
			return api.Authorization{}, &api.ControlError{Status: 403, Message: "policy revoked"}
		}
		if latest.Version != sc.Policy.Version {
			s.VersionConflicts.Add(1)
			continue
		}
		result.Decision = d
		return result, nil
	}
	return api.Authorization{}, &api.ControlError{Status: 503, Message: "policy changed during authorization"}
}

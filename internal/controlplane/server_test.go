package controlplane

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"example.com/auth-demo/internal/api"
	apiv1 "github.com/kagent-dev/kagent/go/api/gen/kagent/api/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	authnv1 "k8s.io/api/authentication/v1"
	authzv1 "k8s.io/api/authorization/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
	ktesting "k8s.io/client-go/testing"
)

const sid = "019a132e-2e49-7000-8000-000000000001"
const spiffe = "spiffe://substrate-actor.local/worker-for-actor/demo/session-" + sid

type sessions struct {
	apiv1.SessionServiceClient
	created, deleted, gets int
	listed                 []*apiv1.Session
	missing                bool
	partial                bool
	lookupErr              error
}

func (s *sessions) CreateSession(context.Context, *apiv1.CreateSessionRequest, ...grpc.CallOption) (*apiv1.CreateSessionResponse, error) {
	s.created++
	return &apiv1.CreateSessionResponse{Session: &apiv1.Session{Id: sid}}, nil
}
func (s *sessions) GetSession(context.Context, *apiv1.GetSessionRequest, ...grpc.CallOption) (*apiv1.GetSessionResponse, error) {
	s.gets++
	if s.lookupErr != nil {
		return nil, s.lookupErr
	}
	if s.missing {
		return nil, status.Error(codes.NotFound, "deleted")
	}
	return &apiv1.GetSessionResponse{Session: &apiv1.Session{Id: sid, Agent: &apiv1.ResourceReference{Namespace: "demo", Name: "mastra-http"}}}, nil
}
func (s *sessions) DeleteSession(context.Context, *apiv1.DeleteSessionRequest, ...grpc.CallOption) (*apiv1.DeleteSessionResponse, error) {
	s.deleted++
	if s.deleted == 1 {
		return nil, status.Error(codes.FailedPrecondition, "quiescing")
	}
	return &apiv1.DeleteSessionResponse{}, nil
}
func (s *sessions) ListSessions(_ context.Context, r *apiv1.ListSessionsRequest, _ ...grpc.CallOption) (*apiv1.ListSessionsResponse, error) {
	if s.partial {
		if r.GetPage().GetPageToken() != "" {
			return nil, status.Error(codes.Unavailable, "listing failed")
		}
		return &apiv1.ListSessionsResponse{Page: &apiv1.PageResponse{NextPageToken: "next"}}, nil
	}
	return &apiv1.ListSessionsResponse{Sessions: s.listed}, nil
}

func eventually(t *testing.T, check func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !check() {
		if time.Now().After(deadline) {
			t.Fatal("watch condition did not converge")
		}
		time.Sleep(5 * time.Millisecond)
	}
}
func fixture(t *testing.T) (*Server, *fake.Clientset, *sessions) {
	t.Helper()
	config := `{"agent":{"namespace":"demo","name":"mastra-http"},"origin":"https://catalog.demo","access":"read","credential":"reader-token"}`
	k := fake.NewSimpleClientset(&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "reader", Namespace: "demo", UID: types.UID("uid"), ResourceVersion: "1", Labels: map[string]string{api.ManagedLabel: "true"}, Annotations: map[string]string{api.PolicyTypeAnnotation: "catalog-access/v1", api.PolicyConfigAnnotation: config}}})
	k.PrependReactor("create", "tokenreviews", func(ktesting.Action) (bool, runtime.Object, error) {
		return true, &authnv1.TokenReview{Status: authnv1.TokenReviewStatus{Authenticated: true, User: authnv1.UserInfo{Username: "alice"}}}, nil
	})
	k.PrependReactor("create", "subjectaccessreviews", func(a ktesting.Action) (bool, runtime.Object, error) {
		r := a.(ktesting.CreateAction).GetObject().(*authzv1.SubjectAccessReview)
		return true, &authzv1.SubjectAccessReview{Status: authzv1.SubjectAccessReviewStatus{Allowed: r.Spec.User == "alice" && r.Spec.ResourceAttributes.Name == "reader"}}, nil
	})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	objects := NewWatchedState(k, "demo")
	if err := objects.Start(ctx); err != nil {
		t.Fatal(err)
	}
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("platform-token"), 0600); err != nil {
		t.Fatal(err)
	}
	up := &sessions{}
	return &Server{Kube: k, Store: newMemoryStore(), Kagent: up, Namespace: "demo", TokenPath: token, Objects: objects, Credentials: &testCredentials{value: "test-secret"}, GCInterval: 5 * time.Minute, BindingRetention: 24 * time.Hour, RequestRetention: 7 * 24 * time.Hour}, k, up
}
func call(t *testing.T, s *Server, path string, input any, client bool) *httptest.ResponseRecorder {
	t.Helper()
	raw, _ := json.Marshal(input)
	r := httptest.NewRequest("POST", path, bytes.NewReader(raw))
	r.Header.Set("Authorization", "Bearer validated-by-tokenreview")
	if client {
		r.TLS = &tls.ConnectionState{VerifiedChains: [][]*x509.Certificate{{{}}}, PeerCertificates: []*x509.Certificate{{Subject: pkix.Name{CommonName: "ext-proc"}}}}
	}
	w := httptest.NewRecorder()
	s.Handler().ServeHTTP(w, r)
	return w
}
func launch(t *testing.T, s *Server) {
	t.Helper()
	w := call(t, s, "/v1/sessions", api.CreateSession{Agent: api.Ref{Namespace: "demo", Name: "mastra-http"}, ServiceAccount: "reader", RequestID: "one"}, false)
	if w.Code != 200 {
		t.Fatal(w.Body)
	}
}
func grant() api.CredentialRequest {
	return api.CredentialRequest{SpiffeID: spiffe, PolicyVersion: "1", Name: "reader-token", Request: api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"}}
}

func TestBridgeLaunchBindingAndPermission(t *testing.T) {
	s, _, up := fixture(t)
	launch(t, s)
	launch(t, s)
	if up.created != 1 {
		t.Fatal("retry duplicated native Session")
	}
	w := call(t, s, "/v1/sessions", api.CreateSession{Agent: api.Ref{Namespace: "demo", Name: "mastra-http"}, ServiceAccount: "admin", RequestID: "one"}, false)
	if w.Code != 403 {
		t.Fatal("unauthorized account selected")
	}
	b, err := s.Store.Binding(sid)
	if err != nil || b.AccountUID != "uid" || b.User != "alice" {
		t.Fatalf("%+v %v", b, err)
	}
}
func TestWatchPolicyChangeUIDReplacementAndLabelRemoval(t *testing.T) {
	s, k, _ := fixture(t)
	launch(t, s)
	ctx := context.Background()
	sa, _ := k.CoreV1().ServiceAccounts("demo").Get(ctx, "reader", metav1.GetOptions{})
	sa.Annotations[api.PolicyTypeAnnotation] = "deny-all/v1"
	sa.ResourceVersion = "2"
	_, _ = k.CoreV1().ServiceAccounts("demo").Update(ctx, sa, metav1.UpdateOptions{})
	eventually(t, func() bool { sc, err := s.resolve(ctx, spiffe); return err == nil && sc.Policy.Type == "deny-all/v1" })
	if w := call(t, s, "/internal/credential", grant(), true); w.Code == 200 {
		t.Fatal("revoked policy released credential")
	}
	sa.UID = "replacement"
	sa.ResourceVersion = "3"
	_, _ = k.CoreV1().ServiceAccounts("demo").Update(ctx, sa, metav1.UpdateOptions{})
	eventually(t, func() bool {
		o, err := s.Objects.Account(ctx, "demo", "reader")
		return err == nil && o.UID == "replacement"
	})
	if _, err := s.resolve(ctx, spiffe); err == nil {
		t.Fatal("UID replacement inherited binding")
	}
	delete(sa.Labels, api.ManagedLabel)
	sa.ResourceVersion = "4"
	_, _ = k.CoreV1().ServiceAccounts("demo").Update(ctx, sa, metav1.UpdateOptions{})
	eventually(t, func() bool { _, err := s.Objects.Account(ctx, "demo", "reader"); return err != nil })
}
func TestCredentialRequiresCertificateAndExactGrant(t *testing.T) {
	s, _, _ := fixture(t)
	launch(t, s)
	r := grant()
	if w := call(t, s, "/internal/credential", r, false); w.Code != 403 {
		t.Fatal("certificate missing")
	}
	if w := call(t, s, "/internal/credential", r, true); w.Code != 200 {
		t.Fatal(w.Body)
	}
	r.Name = "another-secret"
	if w := call(t, s, "/internal/credential", r, true); w.Code != 403 {
		t.Fatal("arbitrary Secret released")
	}
	r = grant()
	r.Request.Host = "attacker.demo"
	if w := call(t, s, "/internal/credential", r, true); w.Code != 403 {
		t.Fatal("credential released for another destination")
	}
}
func TestNoPerRequestKubernetesOrSessionReads(t *testing.T) {
	s, k, up := fixture(t)
	launch(t, s)
	k.ClearActions()
	// Wait beyond the removed TTL, then prove that warm requests still make no GETs.
	time.Sleep(2100 * time.Millisecond)
	for range 20 {
		if w := call(t, s, "/internal/credential", grant(), true); w.Code != 200 {
			t.Fatal(w.Body)
		}
	}
	for _, a := range k.Actions() {
		if a.GetVerb() == "get" || a.GetVerb() == "list" {
			t.Fatalf("warm request contacted Kubernetes: %s %s", a.GetVerb(), a.GetResource().Resource)
		}
	}
	if up.gets != 0 {
		t.Fatal("egress performed a native GetSession")
	}
	k.PrependReactor("get", "*", func(ktesting.Action) (bool, runtime.Object, error) { return true, nil, errors.New("API unavailable") })
	if w := call(t, s, "/internal/credential", grant(), true); w.Code != 200 {
		t.Fatal("known snapshot was expired during disconnection")
	}
	if _, err := s.Objects.Account(context.Background(), "demo", "unknown"); err == nil {
		t.Fatal("unresolved cache miss permitted")
	}
}

type testCredentials struct{ value string }

func (c *testCredentials) Read(context.Context, string, string) (string, error) { return c.value, nil }
func (c *testCredentials) Invalidate(string, string) error                      { return nil }
func (c *testCredentials) Stats() map[string]uint64                             { return map[string]uint64{} }
func TestDeleteRevokesImmediatelyAndAllowsRetry(t *testing.T) {
	s, _, up := fixture(t)
	launch(t, s)
	for _, want := range []int{409, 200} {
		r := httptest.NewRequest(http.MethodDelete, "/v1/sessions/"+sid, nil)
		r.Header.Set("Authorization", "Bearer reviewed")
		w := httptest.NewRecorder()
		s.Handler().ServeHTTP(w, r)
		if w.Code != want {
			t.Fatalf("delete %d want %d", w.Code, want)
		}
		if _, err := s.resolve(context.Background(), spiffe); err == nil {
			t.Fatal("revocation did not stick")
		}
	}
	if up.deleted != 2 {
		t.Fatal("cleanup retry did not reach native API")
	}
}
func TestSessionCleanupRequiresCompleteScanAndKeepsSuspendedSessions(t *testing.T) {
	for _, mode := range []string{"suspended", "partial", "deleted", "missing-from-list", "lookup-error", "newly-created"} {
		t.Run(mode, func(t *testing.T) {
			s, _, up := fixture(t)
			launch(t, s)
			memory := s.Store.(*memoryStore)
			memory.created[sid] = time.Now().Add(-2 * time.Minute)
			up.listed = []*apiv1.Session{{Id: sid, State: apiv1.RuntimeState_RUNTIME_STATE_SUSPENDED}}
			if mode == "partial" {
				up.partial = true
			}
			if mode == "deleted" {
				up.listed = nil
				up.missing = true
			}
			if mode == "missing-from-list" || mode == "lookup-error" {
				up.listed = nil
			}
			if mode == "lookup-error" {
				up.lookupErr = status.Error(codes.Unavailable, "confirmation failed")
			}
			if mode == "newly-created" {
				up.listed = nil
				up.missing = true
				memory.created[sid] = time.Now()
			}
			_, err := s.Reconcile(context.Background())
			b, _ := s.Store.Binding(sid)
			if mode == "partial" || mode == "lookup-error" {
				if err == nil || !b.Active || memory.purges != 0 {
					t.Fatal("partial scan cleaned state")
				}
			} else if err != nil {
				t.Fatal(err)
			} else if mode == "deleted" && b.Active {
				t.Fatal("confirmed deleted Session retained")
			} else if mode != "deleted" && !b.Active {
				t.Fatal("existing or newly created Session revoked")
			}
		})
	}
}

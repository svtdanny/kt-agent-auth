package controlplane

import (
	"context"
	"encoding/json"
	"example.com/auth-demo/internal/api"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"testing"
)

func TestConfigMapPermissionsAndScope(t *testing.T) {
	s, k, _ := fixture(t)
	launch(t, s)
	ctx := context.Background()
	r := api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"}
	result, err := s.Authorize(ctx, spiffe, r)
	if err != nil || !result.Decision.Allowed || result.Decision.Headers["authorization"] != "Bearer test-secret" {
		t.Fatal(result, err)
	}
	cm, _ := k.CoreV1().ConfigMaps("demo").Get(ctx, "reader-policy", metav1.GetOptions{})
	cm.Data["type"] = "deny-all/v1"
	cm.ResourceVersion = "2"
	k.CoreV1().ConfigMaps("demo").Update(ctx, cm, metav1.UpdateOptions{})
	eventually(t, func() bool { result, err := s.Authorize(ctx, spiffe, r); return err == nil && !result.Decision.Allowed })
	// Another authorizer sharing the same store must never accept this Session.
	s.AgentName = "other-agent"
	if _, err := s.Authorize(ctx, spiffe, r); err == nil {
		t.Fatal("cross-agent Session accepted")
	}
	s.AgentName = "mastra-http"
	k.CoreV1().ConfigMaps("demo").Delete(ctx, "reader-policy", metav1.DeleteOptions{})
	eventually(t, func() bool { _, err := s.Authorize(ctx, spiffe, r); return err != nil })
}
func TestConfigMapPolicyVersionIncludesIdentityAndDocument(t *testing.T) {
	s, k, _ := fixture(t)
	launch(t, s)
	ctx := context.Background()
	before, err := s.resolve(ctx, spiffe)
	if err != nil {
		t.Fatal(err)
	}
	cm, _ := k.CoreV1().ConfigMaps("demo").Get(ctx, "reader-policy", metav1.GetOptions{})
	cm.ResourceVersion = "2"
	cm.Data["config"] = string(json.RawMessage(`{}`))
	k.CoreV1().ConfigMaps("demo").Update(ctx, cm, metav1.UpdateOptions{})
	eventually(t, func() bool {
		after, err := s.resolve(ctx, spiffe)
		return err == nil && after.Policy.Version != before.Policy.Version
	})
}

func TestReplacedServiceAccountCannotInheritBinding(t *testing.T) {
	s, k, _ := fixture(t)
	launch(t, s)
	ctx := context.Background()
	sa, _ := k.CoreV1().ServiceAccounts("demo").Get(ctx, "reader", metav1.GetOptions{})
	sa.UID = "new-uid"
	sa.ResourceVersion = "2"
	if _, err := k.CoreV1().ServiceAccounts("demo").Update(ctx, sa, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool {
		_, err := s.Authorize(ctx, spiffe, api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"})
		return err != nil
	})
}

type changingCredentials struct {
	testCredentials
	change func()
}

func (c changingCredentials) Read(context.Context, string, string) (string, error) {
	c.change()
	return "must-not-forward", nil
}
func TestRevocationWhileFetchingCredentialsDeniesRequest(t *testing.T) {
	s, _, _ := fixture(t)
	launch(t, s)
	s.Credentials = &changingCredentials{change: func() {
		if err := s.Store.Deactivate(sid); err != nil {
			t.Fatal(err)
		}
	}}
	if _, err := s.Authorize(context.Background(), spiffe, api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"}); err == nil {
		t.Fatal("revoked Session received credential")
	}
}

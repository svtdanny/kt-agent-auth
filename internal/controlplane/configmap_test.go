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
	// Another project sharing the same store must never accept this Session.
	s.Namespace = "other-project"
	if _, err := s.Authorize(ctx, spiffe, r); err == nil {
		t.Fatal("cross-project Session accepted")
	}
	s.Namespace = "demo"
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

func TestProjectAuthorizerAcceptsMultipleAgentsWithoutLosingIdentity(t *testing.T) {
	s, k, _ := fixture(t)
	launch(t, s)
	ctx := context.Background()
	cm, _ := k.CoreV1().ConfigMaps("demo").Get(ctx, "reader-policy", metav1.GetOptions{})
	cm.Data["type"] = "http-request/v1"
	cm.Data["config"] = `{"origin":"https://catalog.demo","methods":["GET"],"paths":["/catalog"],"credential":"reader-token"}`
	cm.ResourceVersion = "2"
	if _, err := k.CoreV1().ConfigMaps("demo").Update(ctx, cm, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool { c, err := s.resolve(ctx, spiffe); return err == nil && c.Policy.Type == "http-request/v1" })
	memory := s.Store.(*memoryStore)
	other := memory.bindings[sid]
	other.SessionID = "019a132e-2e49-7000-8000-000000000002"
	other.Agent.Name = "second-agent"
	memory.bindings[other.SessionID] = other
	for _, id := range []string{sid, other.SessionID} {
		result, err := s.Authorize(ctx, "spiffe://substrate-actor.local/worker-for-actor/demo/session-"+id, api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"})
		if err != nil || !result.Decision.Allowed || result.Context.Binding.Agent != memory.bindings[id].Agent {
			t.Fatalf("Agent binding lost: %+v %v", result, err)
		}
	}
}

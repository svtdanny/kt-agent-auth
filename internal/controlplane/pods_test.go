package controlplane

import (
	"context"
	"testing"

	"example.com/auth-demo/internal/api"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

func TestPodRegistrationAndWatchedIdentityRevocation(t *testing.T) {
	for _, change := range []string{"deleted", "replaced", "unlabelled", "account-replaced"} {
		t.Run(change, func(t *testing.T) {
			s, k, _ := fixture(t)
			ctx := context.Background()
			p := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "runner", Namespace: "demo", UID: types.UID(sid), Labels: map[string]string{"auth.demo.dev/egress": "enforced"}}, Spec: corev1.PodSpec{ServiceAccountName: "reader"}}
			if _, err := k.CoreV1().Pods("demo").Create(ctx, p, metav1.CreateOptions{}); err != nil {
				t.Fatal(err)
			}
			eventually(t, func() bool { _, err := s.Objects.Pod(ctx, "demo", "runner"); return err == nil })
			input := map[string]string{"namespace": "demo", "name": "runner", "uid": sid}
			if w := call(t, s, "/internal/pods", input, false); w.Code != 403 {
				t.Fatal("untrusted client registered Pod")
			}
			input["uid"] = "forged"
			if w := call(t, s, "/internal/pods", input, true); w.Code != 403 {
				t.Fatal("launcher registered wrong UID")
			}
			input["uid"] = sid
			for range 2 {
				if w := call(t, s, "/internal/pods", input, true); w.Code != 200 {
					t.Fatal(w.Body)
				}
			}
			identity := "spiffe://auth-demo.local/ns/demo/pod/" + sid
			k.ClearActions()
			for range 20 {
				sc, err := s.resolve(ctx, identity)
				if err != nil || sc.Binding.AccountUID != "uid" {
					t.Fatalf("Pod resolution: %+v %v", sc, err)
				}
			}
			for _, action := range k.Actions() {
				if action.GetVerb() == "get" || action.GetVerb() == "list" {
					t.Fatal("warm Pod resolution called Kubernetes")
				}
			}
			switch change {
			case "deleted":
				_ = k.CoreV1().Pods("demo").Delete(ctx, "runner", metav1.DeleteOptions{})
			case "replaced":
				p.UID = "019a132e-2e49-7000-8000-000000000002"
				_, _ = k.CoreV1().Pods("demo").Update(ctx, p, metav1.UpdateOptions{})
			case "unlabelled":
				p.Labels = nil
				_, _ = k.CoreV1().Pods("demo").Update(ctx, p, metav1.UpdateOptions{})
			case "account-replaced":
				sa, _ := k.CoreV1().ServiceAccounts("demo").Get(ctx, "reader", metav1.GetOptions{})
				sa.UID = "replacement"
				_, _ = k.CoreV1().ServiceAccounts("demo").Update(ctx, sa, metav1.UpdateOptions{})
			}
			eventually(t, func() bool { _, err := s.resolve(ctx, identity); return err != nil })
			if change == "account-replaced" {
				if w := call(t, s, "/internal/pods", input, true); w.Code != 409 {
					t.Fatal("re-registration changed immutable account UID")
				}
			}
			if b, err := s.Store.PodBinding(sid); err != nil || b.Account != (api.Ref{Namespace: "demo", Name: "reader"}) {
				t.Fatal("lost debug mapping")
			}
		})
	}
}

package controlplane

import (
	"context"
	"testing"

	"example.com/auth-demo/internal/api"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes/fake"
	ktesting "k8s.io/client-go/testing"
)

func TestExpiredWatchRelistsAndRecoversPolicy(t *testing.T) {
	sa := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Name: "reader", Namespace: "demo", ResourceVersion: "1",
		Labels:      map[string]string{api.ManagedLabel: "true"},
		Annotations: map[string]string{api.PolicyTypeAnnotation: "catalog-access/v1"},
	}}
	k := fake.NewSimpleClientset(sa)
	first := watch.NewRaceFreeFake()
	watchCount := 0
	k.PrependWatchReactor("serviceaccounts", func(ktesting.Action) (bool, watch.Interface, error) {
		watchCount++
		if watchCount == 1 {
			return true, first, nil
		}
		return false, nil, nil
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w := NewWatchedState(k, "demo")
	if err := w.Start(ctx); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool { return w.Stats()["watchCalls"] >= 1 })
	// Change authoritative state while the first watch misses the update, then
	// force resource-version expiry. Reflector recovery must LIST the new state.
	changed := sa.DeepCopy()
	changed.ResourceVersion = "2"
	changed.Annotations[api.PolicyTypeAnnotation] = "deny-all/v1"
	if err := k.Tracker().Update(schema.GroupVersionResource{Version: "v1", Resource: "serviceaccounts"}, changed, "demo"); err != nil {
		t.Fatal(err)
	}
	first.Error(&metav1.Status{Status: metav1.StatusFailure, Reason: metav1.StatusReasonExpired, Code: 410})
	first.Stop()
	eventually(t, func() bool {
		got, err := w.Account(ctx, "demo", "reader")
		return err == nil && got.ResourceVersion == "2" && got.Annotations[api.PolicyTypeAnnotation] == "deny-all/v1"
	})
	if w.Stats()["listCalls"] < 2 || w.Stats()["accountGETs"] != 0 {
		t.Fatalf("watch recovery did not use a relist: %v", w.Stats())
	}
}

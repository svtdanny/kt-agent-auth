package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"example.com/auth-demo/internal/api"
	"golang.org/x/sync/singleflight"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"
)

type ObjectState interface {
	Account(context.Context, string, string) (*corev1.ServiceAccount, error)
	Policy(context.Context, *corev1.ServiceAccount) (api.Policy, error)
	Stats() map[string]uint64
}

// WatchedState has no TTL, periodic resync, or scheduled polling. Reflectors
// perform the initial list and recover/re-list only when the watch requires it.
type WatchedState struct {
	kube                                             kubernetes.Interface
	namespace                                        string
	accounts                                         cache.SharedIndexInformer
	policies                                         cache.SharedIndexInformer
	policyGets                                       atomic.Uint64
	mu                                               sync.Mutex
	missing                                          map[string]bool
	misses                                           singleflight.Group
	accountGets, lists, watches, accountEvents, hits atomic.Uint64
}

func NewWatchedState(k kubernetes.Interface, ns string) *WatchedState {
	w := &WatchedState{kube: k, namespace: ns, missing: map[string]bool{}}
	lw := &cache.ListWatch{
		ListWithContextFunc: func(ctx context.Context, o metav1.ListOptions) (runtime.Object, error) {
			w.lists.Add(1)
			o.LabelSelector = api.ManagedLabel + "=true"
			return k.CoreV1().ServiceAccounts(ns).List(ctx, o)
		},
		WatchFuncWithContext: func(ctx context.Context, o metav1.ListOptions) (watch.Interface, error) {
			w.watches.Add(1)
			o.LabelSelector = api.ManagedLabel + "=true"
			return k.CoreV1().ServiceAccounts(ns).Watch(ctx, o)
		},
	}
	w.accounts = cache.NewSharedIndexInformer(cache.ToListWatcherWithWatchListSemantics(lw, k), &corev1.ServiceAccount{}, 0, cache.Indexers{})
	_, _ = w.accounts.AddEventHandler(cache.ResourceEventHandlerFuncs{AddFunc: func(o any) { w.accountEvent(o, false) }, UpdateFunc: func(_, o any) { w.accountEvent(o, false) }, DeleteFunc: func(o any) { w.accountEvent(o, true) }})
	policyWatch := &cache.ListWatch{
		ListWithContextFunc: func(ctx context.Context, o metav1.ListOptions) (runtime.Object, error) {
			w.lists.Add(1)
			o.LabelSelector = api.ManagedLabel + "=true"
			return k.CoreV1().ConfigMaps(ns).List(ctx, o)
		},
		WatchFuncWithContext: func(ctx context.Context, o metav1.ListOptions) (watch.Interface, error) {
			w.watches.Add(1)
			o.LabelSelector = api.ManagedLabel + "=true"
			return k.CoreV1().ConfigMaps(ns).Watch(ctx, o)
		},
	}
	w.policies = cache.NewSharedIndexInformer(cache.ToListWatcherWithWatchListSemantics(policyWatch, k), &corev1.ConfigMap{}, 0, cache.Indexers{})
	_, _ = w.policies.AddEventHandler(cache.ResourceEventHandlerFuncs{AddFunc: func(o any) { w.mark("policy/"+objectKey(o), false) }, UpdateFunc: func(_, o any) { w.mark("policy/"+objectKey(o), false) }, DeleteFunc: func(o any) { w.mark("policy/"+objectKey(o), true) }})
	return w
}
func (w *WatchedState) Start(ctx context.Context) error {
	go w.accounts.RunWithContext(ctx)
	go w.policies.RunWithContext(ctx)
	ready, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if !cache.WaitForCacheSync(ready.Done(), w.accounts.HasSynced, w.policies.HasSynced) {
		return fmt.Errorf("Policy/ServiceAccount initial synchronization failed: %w", ready.Err())
	}
	return nil
}
func objectKey(obj any) string { key, _ := cache.DeletionHandlingMetaNamespaceKeyFunc(obj); return key }
func (w *WatchedState) mark(key string, missing bool) {
	w.mu.Lock()
	if missing {
		w.missing[key] = true
	} else {
		delete(w.missing, key)
	}
	w.mu.Unlock()
}
func (w *WatchedState) isMissing(key string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.missing[key]
}
func (w *WatchedState) accountEvent(obj any, deleted bool) {
	w.accountEvents.Add(1)
	w.mark("sa/"+objectKey(obj), deleted)
}
func (w *WatchedState) Account(ctx context.Context, ns, name string) (*corev1.ServiceAccount, error) {
	if ns != w.namespace {
		return nil, fmt.Errorf("account is outside the managed namespace")
	}
	key := ns + "/" + name
	if o, exists, err := w.accounts.GetStore().GetByKey(key); err != nil {
		return nil, err
	} else if exists {
		if o.(*corev1.ServiceAccount).Labels[api.ManagedLabel] != "true" {
			return nil, fmt.Errorf("account is not managed")
		}
		w.hits.Add(1)
		return o.(*corev1.ServiceAccount).DeepCopy(), nil
	}
	if w.isMissing("sa/" + key) {
		return nil, fmt.Errorf("account is deleted or no longer managed")
	}
	value, err := sharedRead(ctx, &w.misses, "sa/"+key, func(ctx context.Context) (*corev1.ServiceAccount, error) {
		w.accountGets.Add(1)
		sa, err := w.kube.CoreV1().ServiceAccounts(ns).Get(ctx, name, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			w.mark("sa/"+key, true)
		}
		if err != nil {
			return nil, err
		}
		if sa.Labels[api.ManagedLabel] != "true" {
			w.mark("sa/"+key, true)
			return nil, fmt.Errorf("account is not managed")
		}
		// A delayed GET must never overwrite a newer watch event or resurrect a
		// deleted object, so GET results are not inserted into the watch store.
		return sa, nil
	})
	if err != nil {
		return nil, err
	}
	return value.DeepCopy(), nil
}
func sharedRead[T any](ctx context.Context, g *singleflight.Group, key string, load func(context.Context) (T, error)) (T, error) {
	ch := g.DoChan(key, func() (any, error) {
		r, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		return load(r)
	})
	select {
	case <-ctx.Done():
		var zero T
		return zero, ctx.Err()
	case r := <-ch:
		if r.Err != nil {
			var zero T
			return zero, r.Err
		}
		return r.Val.(T), nil
	}
}
func (w *WatchedState) Stats() map[string]uint64 {
	return map[string]uint64{"policyGETs": w.policyGets.Load(), "accountGETs": w.accountGets.Load(), "listCalls": w.lists.Load(), "watchCalls": w.watches.Load(), "accountEvents": w.accountEvents.Load(), "cacheHits": w.hits.Load()}
}

// Policy is a namespaced ConfigMap reference. Full policy bodies never live on identities.
func (w *WatchedState) Policy(ctx context.Context, sa *corev1.ServiceAccount) (api.Policy, error) {
	name := sa.Annotations[api.PolicyRefAnnotation]
	if sa.Namespace != w.namespace || len(validation.IsDNS1123Subdomain(name)) != 0 {
		return api.Policy{}, fmt.Errorf("account has no policy reference in this scope")
	}
	key := sa.Namespace + "/" + name
	var cm *corev1.ConfigMap
	if o, ok, err := w.policies.GetStore().GetByKey(key); err != nil {
		return api.Policy{}, err
	} else if ok {
		cm = o.(*corev1.ConfigMap)
	} else {
		if w.isMissing("policy/" + key) {
			return api.Policy{}, fmt.Errorf("policy was deleted")
		}
		var err error
		cm, err = sharedRead(ctx, &w.misses, "policy/"+key, func(ctx context.Context) (*corev1.ConfigMap, error) {
			w.policyGets.Add(1)
			cm, err := w.kube.CoreV1().ConfigMaps(sa.Namespace).Get(ctx, name, metav1.GetOptions{})
			if apierrors.IsNotFound(err) || (err == nil && cm.Labels[api.ManagedLabel] != "true") {
				w.mark("policy/"+key, true)
			}
			return cm, err
		})
		if err != nil {
			return api.Policy{}, fmt.Errorf("policy unavailable")
		}
	}
	if cm.Labels[api.ManagedLabel] != "true" || cm.Data["type"] == "" || !json.Valid([]byte(cm.Data["config"])) {
		return api.Policy{}, fmt.Errorf("policy is invalid or unmanaged")
	}
	return api.Policy{Type: cm.Data["type"], Config: json.RawMessage(cm.Data["config"]), Version: string(sa.UID) + ":" + sa.ResourceVersion + ":" + string(cm.UID) + ":" + cm.ResourceVersion}, nil
}

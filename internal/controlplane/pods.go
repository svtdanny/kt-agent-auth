package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"example.com/auth-demo/internal/api"
)

func (s *Server) registerPod(w http.ResponseWriter, r *http.Request) {
	if !internal(r) {
		reject(w, 403, fmt.Errorf("trusted launcher client certificate required"))
		return
	}
	var in struct {
		Namespace string `json:"namespace"`
		Name      string `json:"name"`
		UID       string `json:"uid"`
	}
	if err := decode(w, r, &in); err != nil {
		reject(w, 400, err)
		return
	}
	p, err := s.Objects.Pod(r.Context(), in.Namespace, in.Name)
	if err != nil || string(p.UID) != in.UID || p.DeletionTimestamp != nil {
		reject(w, 403, fmt.Errorf("Pod identity is absent or changed"))
		return
	}
	sa, err := s.Objects.Account(r.Context(), in.Namespace, p.Spec.ServiceAccountName)
	if err != nil {
		reject(w, 403, fmt.Errorf("Pod ServiceAccount is not managed"))
		return
	}
	b := api.Binding{PodName: in.Name, PodUID: in.UID, Atespace: in.Namespace, User: "managed-pod-launcher", Account: api.Ref{Namespace: in.Namespace, Name: sa.Name}, AccountUID: string(sa.UID), Active: true}
	if err = s.Store.RegisterPod(b); err != nil {
		reject(w, 409, err)
		return
	}
	write(w, 200, b)
}

func (s *Server) resolvePod(ctx context.Context, ns, uid string) (api.SessionContext, error) {
	b, err := s.Store.PodBinding(uid)
	if err != nil {
		return api.SessionContext{}, err
	}
	if !b.Active || b.Atespace != ns {
		return api.SessionContext{}, fmt.Errorf("inactive Pod binding")
	}
	p, err := s.Objects.Pod(ctx, ns, b.PodName)
	if err != nil || string(p.UID) != uid || p.DeletionTimestamp != nil || p.Spec.ServiceAccountName != b.Account.Name {
		return api.SessionContext{}, fmt.Errorf("Pod has been deleted or replaced")
	}
	sa, err := s.Objects.Account(ctx, ns, b.Account.Name)
	if err != nil || string(sa.UID) != b.AccountUID {
		return api.SessionContext{}, fmt.Errorf("Pod ServiceAccount has been replaced")
	}
	policy := api.Policy{Type: sa.Annotations[api.PolicyTypeAnnotation], Config: json.RawMessage(sa.Annotations[api.PolicyConfigAnnotation]), Version: sa.ResourceVersion}
	if policy.Type == "" || !json.Valid(policy.Config) {
		return api.SessionContext{}, fmt.Errorf("Pod has no valid policy")
	}
	return api.SessionContext{Binding: b, Policy: policy}, nil
}

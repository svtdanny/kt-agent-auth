package policy

import (
	"context"
	"encoding/json"
	"testing"

	"example.com/auth-demo/internal/api"
)

func TestCatalogAuthorization(t *testing.T) {
	for _, tc := range []struct {
		name, access, method, path, host string
		port                             uint16
		allowed                          bool
	}{
		{"reader read", "read", "GET", "/catalog", "catalog.demo", 443, true},
		{"reader write", "read", "POST", "/orders", "catalog.demo", 443, false},
		{"writer write", "write", "POST", "/orders", "catalog.demo", 443, true},
		{"other host", "write", "GET", "/catalog", "attacker.demo", 443, false},
		{"other port", "write", "GET", "/catalog", "catalog.demo", 8443, false},
		{"encoded path", "write", "GET", "/%63atalog", "catalog.demo", 443, false},
		{"path traversal", "write", "GET", "/private/../catalog", "catalog.demo", 443, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg, _ := json.Marshal(CatalogConfig{Agent: api.Ref{Namespace: "demo", Name: "mastra-http"}, Origin: "https://catalog.demo", Access: tc.access, Credential: "token"})
			s := api.SessionContext{Binding: api.Binding{Active: true, Agent: api.Ref{Namespace: "demo", Name: "mastra-http"}}, Policy: api.Policy{Type: "catalog-access/v1", Config: cfg}}
			r := api.Request{Scheme: "https", Host: tc.host, Port: tc.port, Method: tc.method, Path: tc.path, Headers: map[string]string{"x-service-account": "catalog-writer"}}
			d, err := Evaluate(context.Background(), s, r)
			if err != nil || d.Allowed != tc.allowed {
				t.Fatalf("decision=%+v error=%v", d, err)
			}
			if !d.Allowed && d.Credential != "" {
				t.Fatal("denial exposed a credential reference")
			}
		})
	}
}

func TestUnknownPolicyFailsClosed(t *testing.T) {
	if d, err := Evaluate(context.Background(), api.SessionContext{Policy: api.Policy{Type: "unknown"}}, api.Request{}); err == nil || d.Allowed {
		t.Fatal("unknown policy was not rejected")
	}
}

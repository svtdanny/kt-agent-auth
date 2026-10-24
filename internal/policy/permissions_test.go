package policy

import (
	"context"
	"encoding/json"
	"testing"

	"example.com/auth-demo/internal/api"
)

func permission(id, origin, method, path, key string) Permission {
	raw, _ := json.Marshal(HTTPConfig{Origin: origin, Methods: []string{method}, Paths: []string{path}, Credential: key})
	return Permission{ID: id, Type: "http-request/v1", Config: raw}
}
func TestPermissionSetCombinesWholeGrantsAndRejectsConflicts(t *testing.T) {
	a := permission("catalog", "https://catalog.demo", "GET", "/catalog", "reader")
	b := permission("internal", "http://mcp.tools.svc:8080", "POST", "/mcp", "mcp")
	for _, tc := range []struct {
		name             string
		permissions      []Permission
		request          api.Request
		allowed, invalid bool
		key              string
	}{
		{"empty", nil, api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"}, false, false, ""},
		{"catalog", []Permission{a, b}, api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"}, true, false, "reader"},
		{"internal", []Permission{a, b}, api.Request{Scheme: "http", Host: "mcp.tools.svc", Port: 8080, Method: "POST", Path: "/mcp"}, true, false, "mcp"},
		{"no cross product", []Permission{a, b}, api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "POST", Path: "/mcp"}, false, false, ""},
		{"wrong port", []Permission{b}, api.Request{Scheme: "http", Host: "mcp.tools.svc", Port: 80, Method: "POST", Path: "/mcp"}, false, false, ""},
		{"conflicting keys", []Permission{a, permission("other", "https://catalog.demo", "GET", "/catalog", "writer")}, api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"}, false, true, ""},
		{"duplicate ID", []Permission{a, a}, api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"}, false, true, ""},
		{"unknown sibling", []Permission{a, {ID: "bad", Type: "unknown", Config: json.RawMessage(`{}`)}}, api.Request{Scheme: "https", Host: "catalog.demo", Port: 443, Method: "GET", Path: "/catalog"}, false, true, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			raw, _ := json.Marshal(PermissionSet{Permissions: tc.permissions})
			s := api.SessionContext{Binding: api.Binding{Active: true}, Policy: api.Policy{Type: "permission-set/v1", Config: raw}}
			d, err := Evaluate(context.Background(), s, tc.request)
			if (err != nil) != tc.invalid || d.Allowed != tc.allowed || d.Credential != tc.key {
				t.Fatalf("unexpected permission result: %+v %v", d, err)
			}
		})
	}
}

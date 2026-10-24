// Package policy contains executable policies, independent of Kubernetes and transport.
package policy

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"example.com/auth-demo/internal/api"
)

type Evaluator interface {
	Evaluate(context.Context, api.SessionContext, api.Request) (api.Decision, error)
}

type CatalogConfig struct {
	Agent      api.Ref `json:"agent"`
	Origin     string  `json:"origin"`
	Access     string  `json:"access"`
	Credential string  `json:"credential"`
}

type Catalog struct{}

func ParseCatalog(raw json.RawMessage) (CatalogConfig, error) {
	var c CatalogConfig
	d := json.NewDecoder(strings.NewReader(string(raw)))
	d.DisallowUnknownFields()
	if err := d.Decode(&c); err != nil {
		return c, err
	}
	u, err := url.Parse(c.Origin)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || (u.Port() != "" && u.Port() != "443") {
		return c, fmt.Errorf("catalog origin must be an exact HTTPS origin on port 443")
	}
	if c.Agent.Namespace == "" || c.Agent.Name == "" || c.Credential == "" || (c.Access != "read" && c.Access != "write") {
		return c, fmt.Errorf("invalid catalog policy configuration")
	}
	return c, nil
}

func (Catalog) Evaluate(_ context.Context, s api.SessionContext, r api.Request) (api.Decision, error) {
	c, err := ParseCatalog(s.Policy.Config)
	if err != nil {
		return api.Decision{}, err
	}
	u, _ := url.Parse(c.Origin)
	deny := func(reason string) (api.Decision, error) { return api.Decision{Reason: reason}, nil }
	if !s.Binding.Active || s.Binding.Agent != c.Agent {
		return deny("agent is not permitted by this policy")
	}
	if r.Scheme != "https" || r.Port != 443 || !strings.EqualFold(r.Host, u.Hostname()) {
		return deny("destination is not permitted")
	}
	// Match the original path exactly: encoded separators and path traversal never gain a grant.
	allowed := r.Method == "GET" && r.Path == "/catalog"
	allowed = allowed || c.Access == "write" && r.Method == "POST" && r.Path == "/orders"
	if !allowed {
		return deny("method or path is not permitted")
	}
	return api.Decision{Allowed: true, Reason: "catalog access granted", Credential: c.Credential}, nil
}

func Evaluate(ctx context.Context, s api.SessionContext, r api.Request) (api.Decision, error) {
	switch s.Policy.Type {
	case "permission-set/v1":
		return evaluateSet(ctx, s, r)
	case "http-request/v1":
		return evaluateHTTP(s, r)
	case "catalog-access/v1":
		return (Catalog{}).Evaluate(ctx, s, r)
	case "deny-all/v1":
		return api.Decision{Reason: "service account denies all requests"}, nil
	default:
		return api.Decision{}, fmt.Errorf("unsupported policy type %q", s.Policy.Type)
	}
}

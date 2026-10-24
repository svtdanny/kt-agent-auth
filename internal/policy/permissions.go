package policy

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strconv"
	"strings"

	"example.com/auth-demo/internal/api"
)

type Permission struct {
	ID     string          `json:"id"`
	Type   string          `json:"type"`
	Config json.RawMessage `json:"config"`
}
type PermissionSet struct {
	Permissions []Permission `json:"permissions"`
}
type HTTPConfig struct {
	Origin     string   `json:"origin"`
	Methods    []string `json:"methods"`
	Paths      []string `json:"paths"`
	Credential string   `json:"credential,omitempty"`
}

func strictJSON(raw json.RawMessage, dst any) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(dst); err != nil {
		return err
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return fmt.Errorf("policy must contain one JSON value")
	}
	return nil
}

func parseHTTP(raw json.RawMessage) (HTTPConfig, *url.URL, uint16, error) {
	var c HTTPConfig
	if err := strictJSON(raw, &c); err != nil {
		return c, nil, 0, err
	}
	u, err := url.Parse(c.Origin)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" {
		return c, nil, 0, fmt.Errorf("permission requires an exact HTTP(S) origin")
	}
	port := uint64(80)
	if u.Scheme == "https" {
		port = 443
	}
	if u.Port() != "" {
		port, err = strconv.ParseUint(u.Port(), 10, 16)
		if err != nil || port == 0 {
			return c, nil, 0, fmt.Errorf("invalid permission port")
		}
	}
	if len(c.Methods) == 0 || len(c.Paths) == 0 {
		return c, nil, 0, fmt.Errorf("permission requires methods and exact paths")
	}
	for _, method := range c.Methods {
		if method == "" || strings.ContainsAny(method, " \t\r\n") || strings.ToUpper(method) != method {
			return c, nil, 0, fmt.Errorf("invalid permission method")
		}
	}
	for _, path := range c.Paths {
		parsed, err := url.ParseRequestURI(path)
		if err != nil || !strings.HasPrefix(path, "/") || strings.HasPrefix(path, "//") || parsed.RawQuery != "" || parsed.EscapedPath() != path {
			return c, nil, 0, fmt.Errorf("permission requires exact absolute paths")
		}
	}
	return c, u, uint16(port), nil
}

func evaluateHTTP(s api.SessionContext, r api.Request) (api.Decision, error) {
	c, u, port, err := parseHTTP(s.Policy.Config)
	if err != nil {
		return api.Decision{}, err
	}
	allowed := s.Binding.Active && r.Scheme == u.Scheme && strings.EqualFold(r.Host, u.Hostname()) && r.Port == port && contains(c.Methods, r.Method) && contains(c.Paths, r.Path)
	if !allowed {
		return api.Decision{Reason: "request does not match permission"}, nil
	}
	return api.Decision{Allowed: true, Reason: "HTTP permission granted", Credential: c.Credential}, nil
}

func contains(values []string, value string) bool {
	for _, v := range values {
		if v == value {
			return true
		}
	}
	return false
}

// A set combines complete grants. It never combines a destination from one rule
// with methods, paths or credentials from another; no first-match ordering.
func evaluateSet(ctx context.Context, s api.SessionContext, r api.Request) (api.Decision, error) {
	var set PermissionSet
	if err := strictJSON(s.Policy.Config, &set); err != nil {
		return api.Decision{}, err
	}
	if len(set.Permissions) > 64 {
		return api.Decision{}, fmt.Errorf("too many permissions")
	}
	ids := map[string]bool{}
	result := api.Decision{Reason: "no permission grants this request"}
	for _, p := range set.Permissions {
		if p.ID == "" || ids[p.ID] || (p.Type != "http-request/v1" && p.Type != "catalog-access/v1") {
			return api.Decision{}, fmt.Errorf("invalid permission ID or type")
		}
		ids[p.ID] = true
		child := s
		child.Policy = api.Policy{Type: p.Type, Config: p.Config, Version: s.Policy.Version}
		d, err := Evaluate(ctx, child, r)
		if err != nil {
			return api.Decision{}, err
		}
		if !d.Allowed {
			continue
		}
		if result.Allowed && result.Credential != d.Credential {
			return api.Decision{}, fmt.Errorf("matching permissions grant conflicting credentials")
		}
		result.Allowed = true
		result.Credential = d.Credential
		result.Reason = "permission set grants request"
		result.Permissions = append(result.Permissions, p.ID)
		if result.Headers == nil {
			result.Headers = map[string]string{}
		}
		for key, value := range d.Headers {
			if previous, exists := result.Headers[key]; exists && previous != value {
				return api.Decision{}, fmt.Errorf("matching permissions grant conflicting mutations")
			}
			result.Headers[key] = value
		}
	}
	return result, nil
}

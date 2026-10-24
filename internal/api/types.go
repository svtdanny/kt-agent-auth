package api

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
)

const PolicyTypeAnnotation = "auth.demo.dev/policy-type"
const PolicyConfigAnnotation = "auth.demo.dev/policy-config"
const ManagedLabel = "auth.demo.dev/managed"

type ControlError struct {
	Status  int
	Message string
}

func (e *ControlError) Error() string {
	return fmt.Sprintf("control plane HTTP %d: %s", e.Status, e.Message)
}

type Ref struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

func (r Ref) String() string { return r.Namespace + "/" + r.Name }

type CreateSession struct {
	Agent          Ref    `json:"agent"`
	ServiceAccount string `json:"serviceAccount"`
	RequestID      string `json:"requestId"`
}

type Binding struct {
	PodName    string `json:"podName,omitempty"`
	PodUID     string `json:"podUid,omitempty"`
	SessionID  string `json:"sessionId"`
	Atespace   string `json:"atespace"`
	User       string `json:"user"`
	Agent      Ref    `json:"agent"`
	Account    Ref    `json:"serviceAccount"`
	AccountUID string `json:"serviceAccountUid"`
	ActorUID   string `json:"actorUid,omitempty"`
	Active     bool   `json:"active"`
}

type Policy struct {
	Type    string          `json:"type"`
	Config  json.RawMessage `json:"config"`
	Version string          `json:"version"`
}

type SessionContext struct {
	Binding Binding `json:"binding"`
	Policy  Policy  `json:"policy"`
}

type Request struct {
	Scheme  string            `json:"scheme"`
	Host    string            `json:"host"`
	Port    uint16            `json:"port"`
	Method  string            `json:"method"`
	Path    string            `json:"path"`
	Headers map[string]string `json:"headers"`
}

type Decision struct {
	Permissions []string          `json:"permissions,omitempty"`
	Allowed     bool              `json:"allowed"`
	Reason      string            `json:"reason"`
	Headers     map[string]string `json:"headers,omitempty"`
	Credential  string            `json:"credential,omitempty"`
}

type CredentialRequest struct {
	SpiffeID      string  `json:"spiffeId"`
	PolicyVersion string  `json:"policyVersion"`
	Name          string  `json:"name"`
	Request       Request `json:"request"`
}

type InvokeRequest struct {
	Origin    string            `json:"origin,omitempty"`
	Execution string            `json:"execution,omitempty"`
	Method    string            `json:"method"`
	Path      string            `json:"path"`
	Headers   map[string]string `json:"headers,omitempty"`
	Load      *LoadSpec         `json:"load,omitempty"`
}

// ParsePod accepts the URI SAN issued by our managed-Pod launcher. HTTP headers
// never select this identity; the authenticated gateway supplies the TLS SAN.
func ParsePod(raw string) (string, string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "spiffe" || u.Host != "auth-demo.local" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.RawPath != "" {
		return "", "", fmt.Errorf("invalid Pod identity")
	}
	p := strings.Split(strings.TrimPrefix(u.Path, "/"), "/")
	if len(p) != 4 || p[0] != "ns" || !validName(p[1]) || p[2] != "pod" {
		return "", "", fmt.Errorf("invalid Pod identity")
	}
	// Reuse the strict canonical UUID validation used for native Session IDs.
	_, _, err = ParseActor("spiffe://substrate-actor.local/worker-for-actor/" + p[1] + "/session-" + p[3])
	if err != nil {
		return "", "", err
	}
	return p[1], p[3], nil
}

// LoadSpec bounds the demo's explicit benchmark tool mode.
type LoadSpec struct {
	Concurrency     int `json:"concurrency"`
	DurationSeconds int `json:"durationSeconds"`
}

// ParseActor accepts only the authenticated worker identity used by Substrate CONNECT.
func ParseActor(raw string) (string, string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "spiffe" || u.Host != "substrate-actor.local" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.RawPath != "" {
		return "", "", fmt.Errorf("invalid actor SPIFFE ID")
	}
	parts := strings.Split(strings.TrimPrefix(u.Path, "/"), "/")
	if len(parts) != 3 || parts[0] != "worker-for-actor" || !validName(parts[1]) || !strings.HasPrefix(parts[2], "session-") || !validName(parts[2]) {
		return "", "", fmt.Errorf("invalid session actor identity")
	}
	id := strings.TrimPrefix(parts[2], "session-")
	if len(id) != 36 {
		return "", "", fmt.Errorf("invalid session ID")
	}
	for i, c := range id {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			if c != '-' {
				return "", "", fmt.Errorf("invalid session UUID")
			}
			continue
		}
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return "", "", fmt.Errorf("invalid session UUID")
		}
	}
	return parts[1], id, nil
}

func validName(s string) bool {
	if s == "" || s[0] == '-' || s[len(s)-1] == '-' {
		return false
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-') {
			return false
		}
	}
	return true
}

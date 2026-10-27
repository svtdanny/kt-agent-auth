package api

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
)

const PolicyRefAnnotation = "auth.demo.dev/policy-ref"
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

type Authorization struct {
	Context  SessionContext
	Decision Decision
}

type InvokeRequest struct {
	Origin    string            `json:"origin,omitempty"`
	Execution string            `json:"execution,omitempty"`
	Method    string            `json:"method"`
	Path      string            `json:"path"`
	Headers   map[string]string `json:"headers,omitempty"`
	Load      *LoadSpec         `json:"load,omitempty"`
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

package extproc

import (
	"context"
	"encoding/json"
	"errors"
	"example.com/auth-demo/internal/api"
	"example.com/auth-demo/internal/policy"
	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	extv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	"google.golang.org/protobuf/types/known/structpb"
	"testing"
)

type control struct {
	access           string
	fail, secretFail bool
	released         int
	resolved         int
}

func (c *control) Authorize(ctx context.Context, id string, r api.Request) (api.Authorization, error) {
	c.resolved++
	if c.fail || c.secretFail {
		return api.Authorization{}, errors.New("unavailable")
	}
	cfg, _ := json.Marshal(policy.CatalogConfig{Agent: api.Ref{Namespace: "demo", Name: "mastra-http"}, Origin: "https://catalog.demo", Access: c.access, Credential: "token"})
	sc := api.SessionContext{Binding: api.Binding{Active: true, Agent: api.Ref{Namespace: "demo", Name: "mastra-http"}}, Policy: api.Policy{Type: "catalog-access/v1", Config: cfg}}
	d, err := policy.Evaluate(ctx, sc, r)
	if err != nil {
		return api.Authorization{}, err
	}
	if d.Allowed {
		c.released++
		d.Headers = map[string]string{"authorization": "Bearer gateway-only"}
	}
	return api.Authorization{Context: sc, Decision: d}, nil
}
func message(method, path string) *extv3.ProcessingRequest {
	a, _ := structpb.NewStruct(map[string]any{"xds.filter_chain_name": "egress_tls_mitm", identityAttribute: "spiffe://substrate-actor.local/worker-for-actor/demo/session-019a132e-2e49-7000-8000-000000000001", connectAttribute: "10.0.0.1:443"})
	return &extv3.ProcessingRequest{Attributes: map[string]*structpb.Struct{"auth.agent.ext_proc": a}, Request: &extv3.ProcessingRequest_RequestHeaders{RequestHeaders: &extv3.HttpHeaders{EndOfStream: true, Headers: &corev3.HeaderMap{Headers: []*corev3.HeaderValue{{Key: ":method", Value: method}, {Key: ":authority", Value: "catalog.demo"}, {Key: ":path", Value: path}, {Key: "authorization", RawValue: []byte("sandbox-placeholder")}, {Key: "x-service-account", RawValue: []byte("writer")}}}}}}
}
func TestProcessorDenialAndCredentialRelease(t *testing.T) {
	for _, tc := range []struct {
		name, access, method    string
		fail, secretFail, allow bool
	}{
		{"read allowed", "read", "GET", false, false, true},
		{"spoofed writer rejected", "read", "POST", false, false, false},
		{"writer allowed", "write", "POST", false, false, true},
		{"control plane unavailable", "write", "GET", true, false, false},
		{"secret unavailable", "write", "GET", false, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := &control{access: tc.access, fail: tc.fail, secretFail: tc.secretFail}
			path := "/catalog"
			if tc.method == "POST" {
				path = "/orders"
			}
			r := (&Server{Control: c}).Evaluate(context.Background(), message(tc.method, path))
			if (r.GetRequestHeaders() != nil) != tc.allow {
				t.Fatalf("unexpected response: %v", r)
			}
			if !tc.allow && !tc.secretFail && c.released != 0 {
				t.Fatal("credentials fetched for denied request")
			}
			if tc.allow {
				sets := r.GetRequestHeaders().GetResponse().GetHeaderMutation().GetSetHeaders()
				if len(sets) != 1 || string(sets[0].Header.RawValue) != "Bearer gateway-only" || sets[0].AppendAction != corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD {
					t.Fatal("credential did not overwrite sandbox header")
				}
			}
			if (tc.fail || tc.secretFail) && r.GetImmediateResponse().GetStatus().GetCode() != typev3.StatusCode_ServiceUnavailable {
				t.Fatal("dependency outage was not reported as unavailable")
			}
		})
	}
}

func TestProcessorRejectsMissingTrustedAttributes(t *testing.T) {
	m := message("GET", "/catalog")
	m.Attributes = nil
	c := &control{access: "write"}
	if (&Server{Control: c}).Evaluate(context.Background(), m).GetImmediateResponse() == nil {
		t.Fatal("accepted caller-supplied identity without gateway attributes")
	}
}

func TestHTTPActorAttributesAndNonstandardPorts(t *testing.T) {
	m := message("POST", "/mcp")
	a := m.Attributes["auth.agent.ext_proc"]
	a.Fields[identityAttribute] = structpb.NewStringValue("spiffe://substrate-actor.local/worker-for-actor/demo/session-019a132e-2e49-7000-8000-000000000001")
	a.Fields["xds.filter_chain_name"] = structpb.NewStringValue("egress_cleartext")
	a.Fields[connectAttribute] = structpb.NewStringValue("10.0.0.1:8080")
	m.GetRequestHeaders().Headers.Headers[1].Value = "mock-mcp.test-services.svc.cluster.local:8080"
	_, r, err := parse(m)
	if err != nil || r.Host != "mock-mcp.test-services.svc.cluster.local" || r.Port != 8080 || r.Scheme != "http" {
		t.Fatalf("%+v %v", r, err)
	}
	m.GetRequestHeaders().Headers.Headers[1].Value = "mock-mcp.test-services.svc.cluster.local:8081"
	if _, _, err := parse(m); err == nil {
		t.Fatal("authority overrode actual destination port")
	}
	a.Fields[connectAttribute] = structpb.NewStringValue("10.0.0.1:8080.5")
	if _, _, err := parse(m); err == nil {
		t.Fatal("fractional destination port")
	}
}

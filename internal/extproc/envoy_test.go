package extproc

import (
	"context"
	"testing"

	"example.com/auth-demo/internal/api"
	extv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"google.golang.org/protobuf/types/known/structpb"
)

const testPodIdentity = "spiffe://auth-demo.local/ns/demo/pod/019a132e-2e49-7000-8000-000000000001"

func envoyMessage(chain, id, target string) *extv3.ProcessingRequest {
	m := message("GET", "/catalog")
	a, _ := structpb.NewStruct(map[string]any{"xds.filter_chain_name": chain, identityAttribute: id, connectAttribute: target})
	m.Attributes = map[string]*structpb.Struct{"auth.demo.ext_proc": a}
	return m
}

func TestEnvoyUsesCertificateFilterStateAndActualDestinationPort(t *testing.T) {
	actor := "spiffe://substrate-actor.local/worker-for-actor/demo/session-019a132e-2e49-7000-8000-000000000001"
	for _, tc := range []struct {
		chain, id, target string
		valid             bool
	}{
		{"egress_tls_mitm", actor, "10.0.0.1:443", true},
		{"managed_pod_tls", testPodIdentity, "10.0.0.1:443", true},
		{"managed_pod_http", testPodIdentity, "10.0.0.1:8080", true},
		{"managed_pod_tls", actor, "10.0.0.1:443", false},
		{"egress_tls_mitm", testPodIdentity, "10.0.0.1:443", false},
		{"egress_tls_mitm", actor, "", false},
		{"managed_pod_tls", testPodIdentity, "10.0.0.1:0", false},
		{"unknown", actor, "10.0.0.1:443", false},
	} {
		t.Run(tc.chain+tc.id+tc.target, func(t *testing.T) {
			attrs, err := gatewayAttributes(envoyMessage(tc.chain, tc.id, tc.target))
			if (err == nil) != tc.valid {
				t.Fatalf("valid=%v err=%v", tc.valid, err)
			}
			if tc.valid && attrs.Fields["actorSpiffeId"].GetStringValue() != tc.id {
				t.Fatal("lost trusted identity")
			}
		})
	}
}

type podControl struct {
	control
	inactive bool
}

func (p *podControl) Resolve(context.Context, string) (api.SessionContext, error) {
	return api.SessionContext{Binding: api.Binding{PodUID: "019a132e-2e49-7000-8000-000000000001", Active: !p.inactive}}, nil
}

func TestManagedPodCONNECTOnlyEntersInspectionAndReleasesNoCredentials(t *testing.T) {
	for _, tc := range []struct {
		id, target        string
		inactive, allowed bool
	}{
		{testPodIdentity, "10.0.0.1:443", false, true},
		{testPodIdentity, "10.0.0.1:443", true, false},
		{testPodIdentity, "attacker.example:443", false, false},
		{"spiffe://attacker/ns/demo/pod/019a132e-2e49-7000-8000-000000000001", "10.0.0.1:443", false, false},
	} {
		p := &podControl{inactive: tc.inactive}
		response := (&Server{Control: p}).Evaluate(context.Background(), envoyMessage("managed_pod_connect", tc.id, tc.target))
		if (response.GetRequestHeaders() != nil) != tc.allowed || p.released != 0 {
			t.Fatalf("unsafe tunnel grant: %v", response)
		}
	}
}

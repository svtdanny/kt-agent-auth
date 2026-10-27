package extproc

import (
	"testing"

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

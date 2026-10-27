package extproc

import (
	"fmt"
	"net"
	"strconv"

	"example.com/auth-demo/internal/api"
	extv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"google.golang.org/protobuf/types/known/structpb"
)

const identityAttribute = "filter_state['dev.ate.actor.identity']"
const connectAttribute = "filter_state['dev.ate.connect.authority']"

func attribute(m *extv3.ProcessingRequest, name string) string {
	for _, scope := range m.GetAttributes() {
		if value := scope.GetFields()[name]; value != nil {
			return value.GetStringValue()
		}
	}
	return ""
}

// The outer mTLS listener writes these filter-state objects and shares them
// with the internal listener. Inner HTTP headers cannot set either object.
func gatewayAttributes(m *extv3.ProcessingRequest) (*structpb.Struct, error) {
	chain := attribute(m, "xds.filter_chain_name")
	scheme := ""
	switch chain {
	case "egress_tls_mitm":
		scheme = "https"
	case "egress_cleartext":
		scheme = "http"
	default:
		return nil, fmt.Errorf("unknown gateway filter chain")
	}
	id := attribute(m, identityAttribute)
	if _, _, err := api.ParseActor(id); err != nil {
		return nil, err
	}
	_, rawPort, err := net.SplitHostPort(attribute(m, connectAttribute))
	if err != nil {
		return nil, fmt.Errorf("missing trusted CONNECT destination")
	}
	port, err := strconv.ParseUint(rawPort, 10, 16)
	if err != nil || port == 0 {
		return nil, fmt.Errorf("invalid CONNECT port")
	}
	return structpb.NewStruct(map[string]any{"actorSpiffeId": id, "destinationPort": port, "scheme": scheme})
}

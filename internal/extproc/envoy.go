package extproc

import (
	"context"
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
	if legacy := m.GetAttributes()["envoy.filters.http.ext_proc"]; legacy.GetFields()["actorSpiffeId"] != nil {
		return legacy, nil
	}
	chain := attribute(m, "xds.filter_chain_name")
	scheme := ""
	switch chain {
	case "egress_tls_mitm", "managed_pod_tls":
		scheme = "https"
	case "egress_cleartext", "managed_pod_http":
		scheme = "http"
	default:
		return nil, fmt.Errorf("unknown gateway filter chain")
	}
	id := attribute(m, identityAttribute)
	if chain == "managed_pod_tls" || chain == "managed_pod_http" {
		if _, _, err := api.ParsePod(id); err != nil {
			return nil, err
		}
	} else if _, _, err := api.ParseActor(id); err != nil {
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

func (s *Server) podTunnel(ctx context.Context, m *extv3.ProcessingRequest) *extv3.ProcessingResponse {
	id := attribute(m, identityAttribute)
	if _, _, err := api.ParsePod(id); err != nil {
		return deny("invalid managed Pod certificate")
	}
	host, port, err := net.SplitHostPort(attribute(m, connectAttribute))
	n, portErr := strconv.ParseUint(port, 10, 16)
	if err != nil || portErr != nil || n == 0 || net.ParseIP(host) == nil {
		return deny("invalid CONNECT destination")
	}
	sc, err := s.Control.Resolve(ctx, id)
	if err != nil || !sc.Binding.Active || sc.Binding.PodUID == "" {
		return deny("managed Pod authorization denied")
	}
	// No application upstream is dialed here. Only the private HTTP/TLS
	// inspection listener is reachable after CONNECT; it authorizes every request.
	return &extv3.ProcessingResponse{Response: &extv3.ProcessingResponse_RequestHeaders{RequestHeaders: &extv3.HeadersResponse{Response: &extv3.CommonResponse{}}}}
}

package extproc

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/url"
	"strconv"
	"strings"

	"example.com/auth-demo/internal/api"
	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	extv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	"golang.org/x/net/http/httpguts"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

type Authorizer interface {
	Authorize(context.Context, string, api.Request) (api.Authorization, error)
}
type Server struct {
	extv3.UnimplementedExternalProcessorServer
	Control Authorizer
}

func (s *Server) Process(stream extv3.ExternalProcessor_ProcessServer) error {
	p, ok := peer.FromContext(stream.Context())
	if !ok {
		return status.Error(codes.Unauthenticated, "gateway identity required")
	}
	tls, ok := p.AuthInfo.(credentials.TLSInfo)
	if !ok || len(tls.State.VerifiedChains) == 0 || len(tls.State.PeerCertificates) == 0 || tls.State.PeerCertificates[0].Subject.CommonName != "auth-egress" {
		return status.Error(codes.Unauthenticated, "authenticated gateway required")
	}
	for {
		message, err := stream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if message.GetRequestHeaders() == nil {
			return status.Error(codes.InvalidArgument, "only header processing is configured")
		}
		response := s.Evaluate(stream.Context(), message)
		if err = stream.Send(response); err != nil {
			return err
		}
	}
}
func deny(reason string) *extv3.ProcessingResponse {
	return reject(reason, typev3.StatusCode_Forbidden)
}
func reject(reason string, code typev3.StatusCode) *extv3.ProcessingResponse {
	return &extv3.ProcessingResponse{Response: &extv3.ProcessingResponse_ImmediateResponse{ImmediateResponse: &extv3.ImmediateResponse{Status: &typev3.HttpStatus{Code: code}, Body: []byte("request denied: " + reason), Details: "auth_demo_denied"}}}
}
func (s *Server) Evaluate(ctx context.Context, message *extv3.ProcessingRequest) *extv3.ProcessingResponse {
	id, r, err := parse(message)
	if err != nil {
		return deny("missing or invalid gateway context")
	}
	return s.evaluateRequest(ctx, id, r)
}
func (s *Server) evaluateRequest(ctx context.Context, id string, r api.Request) *extv3.ProcessingResponse {
	result, err := s.Control.Authorize(ctx, id, r)
	if err != nil {
		var control *api.ControlError
		if errors.As(err, &control) && control.Status == 403 {
			return deny("authorization denied")
		}
		return reject("authorization unavailable", typev3.StatusCode_ServiceUnavailable)
	}
	d := result.Decision
	if !d.Allowed {
		return deny(d.Reason)
	}
	headers := d.Headers
	slog.Info("policy decision", "session", result.Context.Binding.SessionID, "agent", result.Context.Binding.Agent.String(), "allowed", true)
	var sets []*corev3.HeaderValueOption
	for name, value := range headers {
		if !safeMutation(name, value) {
			return deny("invalid policy mutation")
		}
		sets = append(sets, &corev3.HeaderValueOption{Header: &corev3.HeaderValue{Key: strings.ToLower(name), RawValue: []byte(value)}, AppendAction: corev3.HeaderValueOption_OVERWRITE_IF_EXISTS_OR_ADD})
	}
	return &extv3.ProcessingResponse{Response: &extv3.ProcessingResponse_RequestHeaders{RequestHeaders: &extv3.HeadersResponse{Response: &extv3.CommonResponse{HeaderMutation: &extv3.HeaderMutation{SetHeaders: sets}}}}}
}
func parse(m *extv3.ProcessingRequest) (string, api.Request, error) {
	attrs, err := gatewayAttributes(m)
	if err != nil {
		return "", api.Request{}, err
	}
	id := attrs.GetFields()["actorSpiffeId"].GetStringValue()
	if _, _, err := api.ParseActor(id); err != nil {
		return "", api.Request{}, err
	}
	port := attrs.GetFields()["destinationPort"].GetNumberValue()
	if port < 1 || port > 65535 || port != float64(uint16(port)) {
		return "", api.Request{}, fmt.Errorf("invalid destination port")
	}
	r := api.Request{Scheme: attrs.GetFields()["scheme"].GetStringValue(), Port: uint16(port), Headers: map[string]string{}}
	for _, h := range m.GetRequestHeaders().GetHeaders().GetHeaders() {
		v := h.Value
		if len(h.RawValue) > 0 {
			v = string(h.RawValue)
		}
		name := strings.ToLower(h.Key)
		if _, exists := r.Headers[name]; exists {
			return "", r, fmt.Errorf("duplicate header %s", name)
		}
		r.Headers[name] = v
	}
	r.Method = r.Headers[":method"]
	authority := r.Headers[":authority"]
	if host, port, err := net.SplitHostPort(authority); err == nil {
		parsed, err := strconv.ParseUint(port, 10, 16)
		if err != nil || parsed != uint64(r.Port) {
			return "", r, fmt.Errorf("unexpected authority port")
		}
		authority = host
	}
	r.Host = authority
	u, err := url.ParseRequestURI(r.Headers[":path"])
	if err != nil {
		return "", r, err
	}
	r.Path = u.EscapedPath()
	if r.Method == "" || r.Host == "" || strings.ContainsAny(r.Host, "/@?#") || (r.Scheme != "https" && r.Scheme != "http") {
		return "", r, fmt.Errorf("invalid HTTP target")
	}
	return id, r, nil
}

func safeMutation(name, value string) bool {
	if !httpguts.ValidHeaderFieldName(name) || !httpguts.ValidHeaderFieldValue(value) {
		return false
	}
	switch strings.ToLower(name) {
	case "host", "connection", "content-length", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate":
		return false
	}
	return true
}

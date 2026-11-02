// Package envoytest tests the configuration with a real, isolated Envoy process.
package envoytest

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"example.com/auth-demo/internal/api"
	"example.com/auth-demo/internal/extproc"
	extv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

type object = map[string]any

type projectPolicy string

func (p projectPolicy) Authorize(_ context.Context, identity string, _ api.Request) (api.Authorization, error) {
	ns, _, err := api.ParseActor(identity)
	if err != nil || ns != string(p) {
		return api.Authorization{}, &api.ControlError{Status: 403, Message: "wrong project"}
	}
	return api.Authorization{Decision: api.Decision{Allowed: true, Headers: map[string]string{"x-project": ns}}}, nil
}

func TestNamespaceRouting(t *testing.T) {
	if os.Getenv("AUTH_DEMO_ENVOY_OVERLAY_TEST") != "1" {
		t.Skip("set AUTH_DEMO_ENVOY_OVERLAY_TEST=1; requires Docker and envoyproxy/envoy:v1.38.6")
	}
	_, source, _, _ := runtime.Caller(0)
	root := filepath.Clean(filepath.Join(filepath.Dir(source), "../.."))
	dir := t.TempDir()
	write := func(name string, value []byte) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(dir, name), value, 0600); err != nil {
			t.Fatal(err)
		}
	}
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test CA"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	der, err := x509.CreateCertificate(rand.Reader, ca, ca, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	ca, _ = x509.ParseCertificate(der)
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(caPEM)
	serial := int64(1)
	issue := func(cn, dns, actor string) (tls.Certificate, []byte, []byte) {
		t.Helper()
		serial++
		leafKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		cert := &x509.Certificate{SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: cn},
			NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
			KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth}}
		if dns != "" {
			cert.DNSNames = []string{dns}
		}
		if actor != "" {
			u, e := url.Parse(actor)
			if e != nil {
				t.Fatal(e)
			}
			cert.URIs = []*url.URL{u}
		}
		data, e := x509.CreateCertificate(rand.Reader, cert, ca, &leafKey.PublicKey, key)
		if e != nil {
			t.Fatal(e)
		}
		private, _ := x509.MarshalECPrivateKey(leafKey)
		certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: data})
		keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: private})
		pair, e := tls.X509KeyPair(certPEM, keyPEM)
		if e != nil {
			t.Fatal(e)
		}
		return pair, certPEM, keyPEM
	}
	_, gatewayCert, gatewayKey := issue("auth-egress", "", "")
	_, frontCert, frontKey := issue("test gateway", "localhost", "")
	write("tls.crt", gatewayCert)
	write("tls.key", gatewayKey)
	write("ca.crt", caPEM)
	write("front.crt", frontCert)
	write("front.key", frontKey)
	startProject := func(ns, ip, serverName string) {
		t.Helper()
		pair, _, _ := issue("authorizer", serverName, "")
		listener, e := net.Listen("tcp", ip+":50052")
		if e != nil {
			t.Fatal(e)
		}
		server := grpc.NewServer(grpc.Creds(credentials.NewTLS(&tls.Config{
			Certificates: []tls.Certificate{pair}, ClientCAs: pool, ClientAuth: tls.RequireAndVerifyClientCert, MinVersion: tls.VersionTLS13})))
		extv3.RegisterExternalProcessorServer(server, &extproc.Server{Control: projectPolicy(ns)})
		go server.Serve(listener)
		t.Cleanup(server.Stop)
	}
	startProject("auth-agent-a", "127.0.0.1", "authorizer-extproc.auth-agent-a.svc")
	startProject("auth-agent-b", "127.0.0.2", "authorizer-extproc.auth-agent-b.svc")
	// This backend chains to the correct CA but has the wrong DNS SAN.
	startProject("auth-agent-wrong", "127.0.0.4", "another-project.svc")
	streamStarted, finishStream := make(chan struct{}), make(chan struct{})
	var finishOnce sync.Once
	t.Cleanup(func() { finishOnce.Do(func() { close(finishStream) }) })
	app := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/stream" {
			fmt.Fprintln(w, r.Header.Get("x-project"))
			w.(http.Flusher).Flush()
			close(streamStarted)
			select {
			case <-finishStream:
				fmt.Fprintln(w, "finished")
			case <-r.Context().Done():
			}
			return
		}
		fmt.Fprint(w, r.Header.Get("x-project"))
	}))
	t.Cleanup(app.Close)
	port := func() int {
		l, e := net.Listen("tcp", "127.0.0.1:0")
		if e != nil {
			t.Fatal(e)
		}
		defer l.Close()
		return l.Addr().(*net.TCPAddr).Port
	}
	frontPort, adminPort := port(), port()
	// Use the exact production fragments; only the native transport/policy leg
	// is replaced by a small TLS fixture to isolate the added routing behavior.
	code := `import sys,json;sys.path.insert(0,'scripts');from namespace_egress_overlay import additions;print(json.dumps(additions()))`
	command := exec.Command("python3", "-c", code)
	command.Dir = root
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("fragments: %v: %s", err, output)
	}
	var fragments struct {
		Processor object
		Listener  object
		Clusters  []object
		Secrets   map[string]object
	}
	if err = json.Unmarshal(output, &fragments); err != nil {
		t.Fatal(err)
	}
	for name, secret := range fragments.Secrets {
		data, _ := json.Marshal(secret)
		write(name, data)
	}
	filterState := object{"name": "envoy.filters.http.set_filter_state", "typed_config": object{
		"@type": "type.googleapis.com/envoy.extensions.filters.http.set_filter_state.v3.Config",
		"on_request_headers": []any{
			object{"object_key": "dev.ate.actor.identity", "factory_key": "envoy.hashable_string", "format_string": object{"text_format_source": object{"inline_string": "%DOWNSTREAM_PEER_URI_SAN%"}}},
			object{"object_key": "dev.ate.connect.authority", "factory_key": "envoy.hashable_string", "format_string": object{"text_format_source": object{"inline_string": "127.0.0.1:8080"}}},
			// Native egress also sets application destination filter state. It
			// must never redirect the independent authorizer DNS lookup.
			object{"object_key": "envoy.upstream.dynamic_host", "shared_with_upstream": "ONCE", "format_string": object{"text_format_source": object{"inline_string": "app.fixture"}}},
			object{"object_key": "envoy.upstream.dynamic_port", "shared_with_upstream": "ONCE", "format_string": object{"text_format_source": object{"inline_string": "8080"}}},
		}}}
	appURL, _ := url.Parse(app.URL)
	_, appPort, _ := net.SplitHostPort(appURL.Host)
	var numericPort int
	fmt.Sscan(appPort, &numericPort)
	clusters := append(fragments.Clusters, object{"name": "app", "type": "STATIC", "connect_timeout": "1s", "load_assignment": object{"cluster_name": "app", "endpoints": []any{object{"lb_endpoints": []any{object{"endpoint": object{"address": object{"socket_address": object{"address": "127.0.0.1", "port_value": numericPort}}}}}}}}})
	config := object{
		"node":                 object{"id": "namespace-overlay-test", "cluster": "test"},
		"admin":                object{"address": object{"socket_address": object{"address": "127.0.0.1", "port_value": adminPort}}},
		"bootstrap_extensions": []any{object{"name": "envoy.bootstrap.internal_listener", "typed_config": object{"@type": "type.googleapis.com/envoy.extensions.bootstrap.internal_listener.v3.InternalListener"}}},
		"static_resources": object{"clusters": clusters, "listeners": []any{fragments.Listener, object{
			"name": "test_ingress", "address": object{"socket_address": object{"address": "127.0.0.1", "port_value": frontPort}},
			"filter_chains": []any{object{"name": "egress_cleartext", "transport_socket": object{"name": "envoy.transport_sockets.tls", "typed_config": object{
				"@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext", "require_client_certificate": true,
				"common_tls_context": object{"tls_certificates": []any{object{"certificate_chain": object{"filename": "/etc/envoy/front.crt"}, "private_key": object{"filename": "/etc/envoy/front.key"}}}, "validation_context": object{"trusted_ca": object{"filename": "/auth-router/ca.crt"}}}}},
				"filters": []any{object{"name": "envoy.filters.network.http_connection_manager", "typed_config": object{
					"@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager", "stat_prefix": "test_ingress",
					"route_config": object{"name": "app", "virtual_hosts": []any{object{"name": "all", "domains": []string{"*"}, "routes": []any{object{"match": object{"prefix": "/"}, "route": object{"cluster": "app", "timeout": "0s"}}}}}},
					"http_filters": []any{filterState, fragments.Processor, object{"name": "envoy.filters.http.router", "typed_config": object{"@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"}}},
				}}}}},
		}}},
	}
	data, _ := json.MarshalIndent(config, "", "  ")
	write("test-envoy.json", data)
	name := fmt.Sprintf("auth-namespace-overlay-%d", time.Now().UnixNano())
	args := []string{"create", "--name", name, "--network=host", "--user=0", "--entrypoint=/usr/local/bin/envoy"}
	for i, ns := range []string{"auth-agent-a", "auth-agent-b", "auth-agent-c", "auth-agent-wrong", "auth-agent-missing"} {
		args = append(args, "--add-host", fmt.Sprintf("authorizer-extproc.%s.svc:127.0.0.%d", ns, i+1))
	}
	args = append(args, "envoyproxy/envoy:v1.38.6", "-c", "/etc/envoy/test-envoy.json", "--concurrency", "1", "--disable-hot-restart", "--log-level", "warning")
	if output, err = exec.Command("docker", args...).CombinedOutput(); err != nil {
		t.Fatalf("docker create: %v: %s", err, output)
	}
	t.Cleanup(func() { _ = exec.Command("docker", "rm", "--force", name).Run() })
	for _, destination := range []string{"/etc/envoy", "/auth-router"} {
		if output, err = exec.Command("docker", "cp", dir+"/.", name+":"+destination).CombinedOutput(); err != nil {
			t.Fatalf("docker cp: %v: %s", err, output)
		}
	}
	var logs bytes.Buffer
	envoy := exec.Command("docker", "start", "--attach", name)
	envoy.Stdout = &logs
	envoy.Stderr = &logs
	if err = envoy.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = exec.Command("docker", "stop", "--time=1", name).Run()
		_ = envoy.Wait()
		if t.Failed() {
			t.Log(logs.String())
		}
	})
	ready := false
	for deadline := time.Now().Add(15 * time.Second); time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
		response, e := http.Get(fmt.Sprintf("http://127.0.0.1:%d/ready", adminPort))
		if e == nil {
			response.Body.Close()
			if response.StatusCode == 200 {
				ready = true
				break
			}
		}
	}
	if !ready {
		t.Fatal("Envoy did not become ready")
	}
	actorID := func(ns string) string {
		return "spiffe://substrate-actor.local/worker-for-actor/" + ns + "/session-019a132e-2e49-7000-8000-000000000001"
	}
	client := func(actor string) *http.Client {
		pair, _, _ := issue("Actor", "", actor)
		transport := &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, Certificates: []tls.Certificate{pair}, ServerName: "localhost"}}
		t.Cleanup(transport.CloseIdleConnections)
		return &http.Client{Transport: transport, Timeout: 15 * time.Second}
	}
	clients := map[string]*http.Client{}
	for _, ns := range []string{"auth-agent-a", "auth-agent-b", "auth-agent-c", "auth-agent-wrong", "auth-agent-missing"} {
		clients[ns] = client(actorID(ns))
	}
	call := func(c *http.Client, path string) (int, string) {
		t.Helper()
		req, _ := http.NewRequest("GET", fmt.Sprintf("https://127.0.0.1:%d%s", frontPort, path), nil)
		req.Host = "app.fixture:8080"
		req.Header.Set("x-auth-verified-actor", actorID("auth-agent-b"))
		req.Header.Set("x-namespace", "auth-agent-b")
		response, e := c.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer response.Body.Close()
		body, _ := io.ReadAll(response.Body)
		return response.StatusCode, string(body)
	}
	for _, ns := range []string{"auth-agent-a", "auth-agent-b"} {
		status, body := call(clients[ns], "/arbitrary/new/path")
		if status != 200 || body != ns {
			t.Fatalf("%s: status=%d body=%q", ns, status, body)
		}
	}
	t.Log("Two project authorizers selected from verified certificate identity; spoofed headers ignored")
	streamRequest, _ := http.NewRequest("GET", fmt.Sprintf("https://127.0.0.1:%d/stream", frontPort), nil)
	streamRequest.Host = "app.fixture:8080"
	response, err := clients["auth-agent-a"].Do(streamRequest)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	select {
	case <-streamStarted:
	case <-time.After(5 * time.Second):
		t.Fatal("stream did not start")
	}
	startProject("auth-agent-c", "127.0.0.3", "authorizer-extproc.auth-agent-c.svc")
	status, body := call(clients["auth-agent-c"], "/new-project")
	if status != 200 || body != "auth-agent-c" {
		t.Fatalf("new namespace: %d %q", status, body)
	}
	finishOnce.Do(func() { close(finishStream) })
	streamBody, err := io.ReadAll(response.Body)
	if err != nil || !strings.Contains(string(streamBody), "finished") {
		t.Fatalf("existing stream interrupted: %v %q", err, streamBody)
	}
	t.Log("Third namespace added after Envoy startup without configuration changes; existing stream completed")
	for _, ns := range []string{"auth-agent-wrong", "auth-agent-missing"} {
		status, _ := call(clients[ns], "/")
		if status < 400 {
			t.Fatalf("%s did not fail closed: %d", ns, status)
		}
	}
	status, _ = call(client("spiffe://substrate-actor.local/worker-for-actor/bad.ns/session-019a132e-2e49-7000-8000-000000000001"), "/")
	if status < 400 {
		t.Fatalf("malformed namespace accepted: %d", status)
	}
	t.Log("Unavailable backend, wrong certificate SAN and malformed namespace fail closed")
}

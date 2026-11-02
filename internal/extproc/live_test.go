package extproc

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	extv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/protobuf/types/known/structpb"
)

// TestLiveReplicas sends four requests through real authorizers, using the test
// gateway identity. scripts/test_scaling.py supplies per-Pod port forwards.
func TestLiveReplicas(t *testing.T) {
	addresses := os.Getenv("AUTH_DEMO_REPLICA_ADDRS")
	if addresses == "" {
		t.Skip("run scripts/test_scaling.py for the live replica check")
	}
	dir := os.Getenv("AUTH_DEMO_PROJECT_TLS")
	ca, err := os.ReadFile(filepath.Join(dir, "ca.crt"))
	if err != nil {
		t.Fatal(err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		t.Fatal("invalid test CA")
	}
	cert, err := tls.LoadX509KeyPair(filepath.Join(dir, "auth-egress.crt"), filepath.Join(dir, "auth-egress.key"))
	if err != nil {
		t.Fatal(err)
	}
	externalCA, err := os.ReadFile(os.Getenv("AUTH_DEMO_EXTERNAL_CA"))
	if err != nil {
		t.Fatal(err)
	}
	externalRoots := x509.NewCertPool()
	externalRoots.AppendCertsFromPEM(externalCA)
	httpClient := &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: externalRoots, MinVersion: tls.VersionTLS12}}}
	defer httpClient.CloseIdleConnections()
	var mu sync.Mutex
	var tokens []string
	var wg sync.WaitGroup
	for _, address := range strings.Split(addresses, ",") {
		conn, err := grpc.NewClient(address, grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{RootCAs: roots, Certificates: []tls.Certificate{cert}, ServerName: "authorizer", MinVersion: tls.VersionTLS13})))
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		for range 2 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
				defer cancel()
				stream, e := extv3.NewExternalProcessorClient(conn).Process(ctx)
				if e != nil {
					t.Error(e)
					return
				}
				attrs, _ := structpb.NewStruct(map[string]any{"xds.filter_chain_name": "egress_tls_mitm", identityAttribute: os.Getenv("AUTH_DEMO_ACTOR"), connectAttribute: "127.0.0.1:8443"})
				message := &extv3.ProcessingRequest{Attributes: map[string]*structpb.Struct{"auth.agent.ext_proc": attrs}, Request: &extv3.ProcessingRequest_RequestHeaders{RequestHeaders: &extv3.HttpHeaders{EndOfStream: true, Headers: &corev3.HeaderMap{Headers: []*corev3.HeaderValue{{Key: ":method", Value: "GET"}, {Key: ":authority", Value: "dedicated-api.auth-demo.test:8443"}, {Key: ":path", Value: "/external"}}}}}}
				if e = stream.Send(message); e != nil {
					t.Error(e)
					return
				}
				response, e := stream.Recv()
				if e != nil {
					t.Error(e)
					return
				}
				_ = stream.CloseSend()
				var value string
				for _, h := range response.GetRequestHeaders().GetResponse().GetHeaderMutation().GetSetHeaders() {
					if h.Header.Key == "authorization" {
						value = string(h.Header.RawValue)
					}
				}
				if value == "" {
					t.Error("authorizer did not release a credential")
					return
				}
				req, _ := http.NewRequestWithContext(ctx, "GET", os.Getenv("AUTH_DEMO_EXTERNAL_URL")+"/external", nil)
				req.Header.Set("Authorization", value)
				resp, e := httpClient.Do(req)
				if e != nil {
					t.Error(e)
					return
				}
				defer resp.Body.Close()
				var event struct {
					Credential string `json:"credential"`
					Version    int    `json:"credentialVersion"`
				}
				if e = json.NewDecoder(resp.Body).Decode(&event); e != nil || resp.StatusCode != 200 || event.Credential != "reader" || event.Version < 1 {
					t.Error("external system rejected credential")
					return
				}
				mu.Lock()
				tokens = append(tokens, value)
				mu.Unlock()
			}()
		}
	}
	wg.Wait()
	if len(tokens) != len(strings.Split(addresses, ","))*2 {
		t.Fatal("some replica requests failed")
	}
	for _, value := range tokens {
		if value != tokens[0] {
			t.Fatal("replicas released different credentials")
		}
	}
	hash := sha256.Sum256([]byte(tokens[0]))
	fmt.Println("TOKEN_FINGERPRINT=" + hex.EncodeToString(hash[:]))
}

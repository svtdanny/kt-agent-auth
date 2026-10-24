// benchmark runs a bounded, closed-loop load test against the deployed demo.
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"example.com/auth-demo/internal/api"
	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	extv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	apiv1 "github.com/kagent-dev/kagent/go/api/gen/kagent/api/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/structpb"
)

const bridge = "https://auth-control-plane.demo.svc.cluster.local:8443"
const origin = "catalog-api.demo.svc.cluster.local"

type runner struct {
	controlHTTP                     *http.Client
	authorityBefore, authorityAfter map[string]uint64
	http                            *http.Client
	token                           string
	grpc                            extv3.ExternalProcessorClient
	sessions                        []string
	stages                          []map[string]any
}

func (r *runner) authorityStats() (map[string]uint64, error) {
	response, err := r.controlHTTP.Get(bridge + "/internal/stats")
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return nil, fmt.Errorf("stats HTTP %d", response.StatusCode)
	}
	var result map[string]uint64
	err = json.NewDecoder(response.Body).Decode(&result)
	return result, err
}

func (r *runner) call(method, path string, body any) (int, map[string]any, error) {
	var data []byte
	if body != nil {
		data, _ = json.Marshal(body)
	}
	req, err := http.NewRequest(method, bridge+path, bytes.NewReader(data))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+r.token)
	response, err := r.http.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer response.Body.Close()
	var out map[string]any
	err = json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&out)
	return response.StatusCode, out, err
}

func (r *runner) create(account, requestID string) (string, error) {
	status, body, err := r.call("POST", "/v1/sessions", api.CreateSession{Agent: api.Ref{Namespace: "demo", Name: "mastra-http"}, ServiceAccount: account, RequestID: requestID})
	if err != nil {
		return "", err
	}
	if status != 200 {
		return "", fmt.Errorf("create HTTP %d: %v", status, body)
	}
	id, ok := body["sessionId"].(string)
	if !ok {
		return "", fmt.Errorf("session ID missing")
	}
	return id, nil
}

func loadResult(value any) map[string]any {
	switch value := value.(type) {
	case map[string]any:
		if m, ok := value["load"].(map[string]any); ok && m["completed"] != nil {
			return m
		}
		for key, child := range value {
			if key == "text" {
				if text, ok := child.(string); ok {
					var decoded any
					if json.Unmarshal([]byte(text), &decoded) == nil {
						if result := loadResult(decoded); result != nil {
							return result
						}
					}
				}
			}
			if result := loadResult(child); result != nil {
				return result
			}
		}
	case []any:
		for _, child := range value {
			if result := loadResult(child); result != nil {
				return result
			}
		}
	}
	return nil
}

func singleResult(value any) map[string]any {
	switch value := value.(type) {
	case map[string]any:
		if calls, ok := value["calls"].([]any); ok && len(calls) == 1 {
			result, _ := calls[0].(map[string]any)
			return result
		}
		for key, child := range value {
			if key == "text" {
				if text, ok := child.(string); ok {
					var decoded any
					if json.Unmarshal([]byte(text), &decoded) == nil {
						if result := singleResult(decoded); result != nil {
							return result
						}
					}
				}
			}
			if result := singleResult(child); result != nil {
				return result
			}
		}
	case []any:
		for _, child := range value {
			if result := singleResult(child); result != nil {
				return result
			}
		}
	}
	return nil
}

func (r *runner) invoke(id string, input api.InvokeRequest) (map[string]any, error) {
	status, body, err := r.call("POST", "/v1/sessions/"+id+"/invoke", input)
	if err != nil {
		return nil, err
	}
	if status != 200 {
		return nil, fmt.Errorf("invoke HTTP %d: %v", status, body)
	}
	return body, nil
}

func (r *runner) auditCount() (int, error) {
	response, err := r.http.Get("https://" + origin + "/audit")
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return 0, fmt.Errorf("audit HTTP %d", response.StatusCode)
	}
	var events []json.RawMessage
	err = json.NewDecoder(io.LimitReader(response.Body, 32<<20)).Decode(&events)
	return len(events), err
}

func (r *runner) fullStage(id, account, method, path string, concurrency, duration int, forbidden bool) error {
	name := fmt.Sprintf("mastra/%s/%s/c%d", account, method, concurrency)
	fmt.Println("STAGE", name)
	before, err := r.auditCount()
	if err != nil {
		return err
	}
	start := time.Now()
	body, err := r.invoke(id, api.InvokeRequest{Method: method, Path: path, Headers: map[string]string{"X-Service-Account": "catalog-writer"}, Load: &api.LoadSpec{Concurrency: concurrency, DurationSeconds: duration}})
	if err != nil {
		return err
	}
	stats := loadResult(body)
	if stats == nil {
		return fmt.Errorf("Mastra load summary missing: %v", body)
	}
	after, err := r.auditCount()
	if err != nil {
		return err
	}
	stats["name"], stats["path"], stats["wallSecondsIncludingA2A"] = name, "full Mastra → transparent gateway → ext_proc → bridge → HTTPS API", time.Since(start).Seconds()
	stats["upstreamAuditDelta"], stats["forbiddenWorkload"] = after-before, forbidden
	statuses, _ := stats["statuses"].(map[string]any)
	allowed, _ := statuses["200"].(float64)
	elapsed, _ := stats["elapsedSeconds"].(float64)
	stats["allowedRequestsPerSecond"] = allowed / elapsed
	stats["upstreamCountMatchesSuccesses"] = float64(after-before) == allowed
	stats["unexpectedAllows"] = 0
	if forbidden {
		stats["unexpectedAllows"] = int(allowed)
	}
	expectedCredential := "readerCredentials"
	if account == "writer" {
		expectedCredential = "writerCredentials"
	}
	actualCredential, _ := stats[expectedCredential].(float64)
	stats["wrongCredentialCount"] = int(allowed - actualCredential)
	r.stages = append(r.stages, stats)
	encoded, _ := json.Marshal(stats)
	fmt.Println(string(encoded))
	return nil
}

func processingRequest(id string, forbidden bool) *extv3.ProcessingRequest {
	attrs, _ := structpb.NewStruct(map[string]any{"actorSpiffeId": "spiffe://substrate-actor.local/worker-for-actor/demo/session-" + id, "destinationPort": 443, "scheme": "https"})
	method, path := "GET", "/catalog"
	if forbidden {
		method, path = "POST", "/orders"
	}
	return &extv3.ProcessingRequest{Attributes: map[string]*structpb.Struct{"envoy.filters.http.ext_proc": attrs}, Request: &extv3.ProcessingRequest_RequestHeaders{RequestHeaders: &extv3.HttpHeaders{EndOfStream: true, Headers: &corev3.HeaderMap{Headers: []*corev3.HeaderValue{
		{Key: ":method", Value: method}, {Key: ":authority", Value: origin}, {Key: ":path", Value: path}, {Key: "authorization", RawValue: []byte("sandbox-placeholder")}, {Key: "x-service-account", RawValue: []byte("catalog-writer")},
	}}}}}
}

func (r *runner) directStage(id string, concurrency int) {
	name := fmt.Sprintf("direct-extproc/mixed/c%d", concurrency)
	fmt.Println("STAGE", name)
	start := time.Now()
	deadline := start.Add(5 * time.Second)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var latencies []float64
	counts := map[string]int{"allows": 0, "expectedDenials": 0, "unexpectedDenials": 0, "unexpectedAllows": 0, "transportErrors": 0, "missingCredentialMutations": 0}
	for worker := 0; worker < concurrency; worker++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			for iteration := 0; time.Now().Before(deadline); iteration++ {
				forbidden := (worker+iteration)%2 == 0
				ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
				began := time.Now()
				stream, err := r.grpc.Process(ctx)
				var response *extv3.ProcessingResponse
				if err == nil {
					err = stream.Send(processingRequest(id, forbidden))
				}
				if err == nil {
					err = stream.CloseSend()
				}
				if err == nil {
					response, err = stream.Recv()
				}
				cancel()
				mu.Lock()
				latencies = append(latencies, float64(time.Since(began).Microseconds())/1000)
				switch {
				case err != nil:
					counts["transportErrors"]++
				case response.GetRequestHeaders() != nil:
					if forbidden {
						counts["unexpectedAllows"]++
					} else {
						counts["allows"]++
					}
					found := false
					for _, h := range response.GetRequestHeaders().GetResponse().GetHeaderMutation().GetSetHeaders() {
						if h.GetHeader().GetKey() == "authorization" && len(h.GetHeader().GetRawValue()) > 0 {
							found = true
						}
					}
					if !found {
						counts["missingCredentialMutations"]++
					}
				case response.GetImmediateResponse().GetStatus().GetCode() == 403:
					if forbidden {
						counts["expectedDenials"]++
					} else {
						counts["unexpectedDenials"]++
					}
				default:
					counts["transportErrors"]++
				}
				mu.Unlock()
			}
		}(worker)
	}
	wg.Wait()
	elapsed := time.Since(start).Seconds()
	sort.Float64s(latencies)
	percentile := func(p float64) float64 {
		if len(latencies) == 0 {
			return 0
		}
		return latencies[int(math.Ceil(p*float64(len(latencies))))-1]
	}
	stats := map[string]any{"name": name, "path": "direct mTLS ext_proc → bridge/PostgreSQL + policy/Vault caches (gateway and upstream excluded)", "concurrency": concurrency, "offeredDurationSeconds": 5, "elapsedSeconds": elapsed, "completed": len(latencies), "requestsPerSecond": float64(len(latencies)) / elapsed, "counts": counts, "latencyMs": map[string]float64{"p50": percentile(.5), "p95": percentile(.95), "p99": percentile(.99), "max": percentile(1)}}
	r.stages = append(r.stages, stats)
	encoded, _ := json.Marshal(stats)
	fmt.Println(string(encoded))
}

func (r *runner) cleanup() []string {
	errors := []string{}
	for _, id := range r.sessions {
		for attempt := 0; attempt < 30; attempt++ {
			status, _, err := r.call("DELETE", "/v1/sessions/"+id, nil)
			if err == nil && status == 200 {
				break
			}
			if status != 409 || attempt == 29 {
				errors = append(errors, fmt.Sprintf("%s HTTP %d: %v", id, status, err))
				break
			}
			time.Sleep(time.Second)
		}
	}
	return errors
}

func (r *runner) recovery(id string) {
	fmt.Println("STAGE recovery/three-successes")
	start := time.Now()
	consecutive := 0
	attempts := []map[string]any{}
	for time.Since(start) < 60*time.Second && consecutive < 3 {
		body, err := r.invoke(id, api.InvokeRequest{Method: "GET", Path: "/catalog"})
		attempt := map[string]any{"elapsedSeconds": time.Since(start).Seconds()}
		call := singleResult(body)
		if err != nil {
			attempt["error"] = err.Error()
			consecutive = 0
		} else {
			attempt["httpStatus"] = call["status"]
			var event map[string]any
			text, _ := call["body"].(string)
			_ = json.Unmarshal([]byte(text), &event)
			if call["status"] == float64(200) && event["credential"] == "reader" && event["credentialAccepted"] == true {
				consecutive++
			} else {
				consecutive = 0
			}
		}
		attempts = append(attempts, attempt)
		if consecutive < 3 {
			time.Sleep(2 * time.Second)
		}
	}
	r.stages = append(r.stages, map[string]any{"name": "recovery/three-successes", "recovered": consecutive == 3, "elapsedSeconds": time.Since(start).Seconds(), "attempts": attempts})
}

func (r *runner) run() error {
	runID := fmt.Sprintf("benchmark-%d", time.Now().UnixNano())
	reader, err := r.create("catalog-reader", runID+"-reader")
	if err != nil {
		return err
	}
	r.sessions = append(r.sessions, reader)
	writer, err := r.create("catalog-writer", runID+"-writer")
	if err != nil {
		return err
	}
	r.sessions = append(r.sessions, writer)
	for _, warm := range []struct{ id, method, path string }{{reader, "GET", "/catalog"}, {writer, "POST", "/orders"}} {
		start := time.Now()
		response, err := r.invoke(warm.id, api.InvokeRequest{Method: warm.method, Path: warm.path})
		if err != nil {
			return err
		}
		call := singleResult(response)
		if call == nil || call["status"] != float64(200) {
			return fmt.Errorf("warmup did not reach API: %v", response)
		}
		r.stages = append(r.stages, map[string]any{"name": "cold-session/" + warm.method, "wallSecondsIncludingA2A": time.Since(start).Seconds()})
	}
	// Full transparent HTTP path first, then isolate the external processor.
	r.authorityBefore, err = r.authorityStats()
	if err != nil {
		return err
	}
	for _, concurrency := range []int{1, 4, 16, 64} {
		duration := 6
		if concurrency == 1 {
			duration = 20
		}
		if err = r.fullStage(reader, "reader", "GET", "/catalog", concurrency, duration, false); err != nil {
			return err
		}
	}
	if err = r.fullStage(reader, "reader", "POST", "/orders", 16, 6, true); err != nil {
		return err
	}
	if err = r.fullStage(writer, "writer", "POST", "/orders", 4, 6, false); err != nil {
		return err
	}
	for _, concurrency := range []int{1, 8, 32, 128} {
		r.directStage(reader, concurrency)
	}
	// Recovery after saturation, through the complete path.
	r.recovery(reader)
	r.authorityAfter, err = r.authorityStats()
	return err
}

func main() {
	deleteNative := flag.String("delete-native-session", "", "test helper: delete a native Session outside the bridge")
	flag.Parse()
	if *deleteNative != "" {
		nativeDelete(*deleteNative)
		return
	}
	ca, err := os.ReadFile("/tls/ca.crt")
	if err != nil {
		panic(err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		panic("invalid CA")
	}
	cert, err := tls.LoadX509KeyPair("/tls/tls.crt", "/tls/tls.key")
	if err != nil {
		panic(err)
	}
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS13, RootCAs: roots, Certificates: []tls.Certificate{cert}}
	controlCert, err := tls.LoadX509KeyPair("/control-tls/tls.crt", "/control-tls/tls.key")
	if err != nil {
		panic(err)
	}
	controlTLS := &tls.Config{MinVersion: tls.VersionTLS13, RootCAs: roots, Certificates: []tls.Certificate{controlCert}}
	token, err := os.ReadFile("/token/token")
	if err != nil {
		panic(err)
	}
	conn, err := grpc.NewClient("auth-ext-proc.demo.svc.cluster.local:50051", grpc.WithTransportCredentials(credentials.NewTLS(tlsConfig)))
	if err != nil {
		panic(err)
	}
	defer conn.Close()
	r := &runner{token: strings.TrimSpace(string(token)), http: &http.Client{Timeout: 180 * time.Second, Transport: &http.Transport{TLSClientConfig: tlsConfig, MaxIdleConns: 256, MaxIdleConnsPerHost: 128}}, grpc: extv3.NewExternalProcessorClient(conn)}
	r.controlHTTP = &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{TLSClientConfig: controlTLS}}
	started := time.Now()
	runErr := r.run()
	cleanupErrors := r.cleanup()
	report := map[string]any{"startedAtUTC": started.UTC().Format(time.RFC3339), "elapsedSeconds": time.Since(started).Seconds(), "loadModel": "bounded closed-loop concurrency; elapsed includes drain; percentiles include failures; no retries of timed requests", "stages": r.stages, "cleanupErrors": cleanupErrors, "completed": runErr == nil}
	delta := map[string]uint64{}
	for key, after := range r.authorityAfter {
		delta[key] = after - r.authorityBefore[key]
	}
	report["authorityTraffic"] = map[string]any{"before": r.authorityBefore, "after": r.authorityAfter, "delta": delta}
	if runErr != nil {
		report["error"] = runErr.Error()
	}
	encoded, _ := json.Marshal(report)
	fmt.Println("BENCHMARK_RESULT", string(encoded))
	if runErr != nil || len(cleanupErrors) > 0 {
		os.Exit(1)
	}
}

func nativeDelete(id string) {
	token, err := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/token")
	if err != nil {
		panic(err)
	}
	connection, err := grpc.NewClient("kagent-controller.kagent.svc:8083", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		panic(err)
	}
	defer connection.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer "+strings.TrimSpace(string(token))))
	client := apiv1.NewSessionServiceClient(connection)
	for range 30 {
		_, err = client.DeleteSession(ctx, &apiv1.DeleteSessionRequest{SessionId: id})
		if err == nil || status.Code(err) == codes.NotFound {
			fmt.Println("Native Session deleted:", id)
			return
		}
		if status.Code(err) != codes.FailedPrecondition && status.Code(err) != codes.Aborted {
			panic(err)
		}
		time.Sleep(time.Second)
	}
	panic(err)
}

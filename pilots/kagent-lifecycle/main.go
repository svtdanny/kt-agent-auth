// Administrative test helper: invoke unchanged native Session lifecycle APIs.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	apiv1 "github.com/kagent-dev/kagent/go/api/gen/kagent/api/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func main() {
	action := flag.String("action", "get", "get, suspend, or resume")
	id := flag.String("session", "", "native Session ID")
	flag.Parse()
	token, err := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/token")
	if err != nil {
		panic(err)
	}
	conn, err := grpc.NewClient("kagent-controller.kagent.svc:8083", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		panic(err)
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer "+strings.TrimSpace(string(token))))
	client := apiv1.NewSessionServiceClient(conn)
	start := time.Now()
	for {
		var session *apiv1.Session
		switch *action {
		case "get":
			var response *apiv1.GetSessionResponse
			response, err = client.GetSession(ctx, &apiv1.GetSessionRequest{SessionId: *id})
			if response != nil {
				session = response.GetSession()
			}
		case "suspend":
			var response *apiv1.SuspendSessionResponse
			response, err = client.SuspendSession(ctx, &apiv1.SuspendSessionRequest{SessionId: *id})
			if response != nil {
				session = response.GetSession()
			}
		case "resume":
			var response *apiv1.ResumeSessionResponse
			response, err = client.ResumeSession(ctx, &apiv1.ResumeSessionRequest{SessionId: *id})
			if response != nil {
				session = response.GetSession()
			}
		default:
			panic("unknown action")
		}
		if err == nil {
			_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"sessionId": session.GetId(), "state": session.GetState().String(),
				"operation": session.GetOperation().String(), "elapsedSeconds": time.Since(start).Seconds()})
			return
		}
		if ctx.Err() != nil || (status.Code(err) != codes.FailedPrecondition && status.Code(err) != codes.Aborted) {
			fmt.Fprintln(os.Stderr, "native lifecycle failed:", status.Code(err))
			os.Exit(1)
		}
		time.Sleep(time.Second)
	}
}

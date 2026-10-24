// The adapter implements kagent's supported BYO contract and invokes Mastra over loopback.
package main

import (
	"bytes"
	"cmp"
	"context"
	"fmt"
	"io"
	"iter"
	"log"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"

	a2atype "github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	"github.com/kagent-dev/kagent/go/adk/pkg/app"
	"github.com/kagent-dev/kagent/go/adk/pkg/config"
)

type executor struct{}

func (executor) Execute(ctx context.Context, r *a2asrv.ExecutorContext) iter.Seq2[a2atype.Event, error] {
	return func(yield func(a2atype.Event, error) bool) {
		if !yield(a2atype.NewSubmittedTask(r, r.Message), nil) {
			return
		}
		var input strings.Builder
		for _, p := range r.Message.Parts {
			input.WriteString(p.Text())
		}
		req, err := http.NewRequestWithContext(ctx, "POST", "http://127.0.0.1:3000/invoke", bytes.NewBufferString(input.String()))
		if err != nil {
			yield(nil, err)
			return
		}
		req.Header.Set("Content-Type", "application/json")
		response, err := (&http.Client{Timeout: 45 * time.Second}).Do(req)
		if err != nil {
			yield(nil, err)
			return
		}
		defer response.Body.Close()
		body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		if err != nil {
			yield(nil, err)
			return
		}
		message := a2atype.NewMessage(a2atype.MessageRoleAgent, a2atype.NewTextPart(string(body)))
		message.ContextID, message.TaskID = r.ContextID, r.TaskID
		yield(a2atype.NewStatusUpdateEvent(r, a2atype.TaskStateCompleted, message), nil)
	}
}
func (executor) Cancel(context.Context, *a2asrv.ExecutorContext) iter.Seq2[a2atype.Event, error] {
	return func(func(a2atype.Event, error) bool) {}
}
func main() {
	if err := config.MaterializeFromEnv("/config"); err != nil {
		log.Fatal(err)
	}
	compiled, err := config.LoadAgentConfig("/config/config.json")
	if err != nil {
		log.Fatal(err)
	}
	runtime := config.ProcessConfig(compiled)
	child := exec.Command("node", "/app/agent/server.mjs")
	child.Stdout, child.Stderr = os.Stdout, os.Stderr
	if err = child.Start(); err != nil {
		log.Fatal(err)
	}
	go func() {
		log.Fatalf("Mastra process exited: %v", child.Wait())
	}()
	// The SDK's separate readiness listener reports success once it starts.
	// Gate SDK startup on Mastra so a native actor wake cannot race Node imports.
	client := &http.Client{Timeout: time.Second}
	deadline := time.Now().Add(60 * time.Second)
	for {
		resp, err := client.Get("http://127.0.0.1:3000/readyz")
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				break
			}
		}
		if time.Now().After(deadline) {
			log.Fatal("Mastra did not become ready within 60 seconds")
		}
		time.Sleep(100 * time.Millisecond)
	}
	health := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp, err := (&http.Client{Timeout: time.Second}).Get("http://127.0.0.1:3000/readyz")
		if err != nil {
			http.Error(w, "starting", 503)
			return
		}
		resp.Body.Close()
		w.WriteHeader(resp.StatusCode)
	})
	a, err := app.New(app.AppConfig{ControlPlaneURL: runtime.ControlPlaneUrl, Port: fmt.Sprint(cmp.Or(runtime.GetPort(), 80)), AppName: runtime.Name, AgentCard: a2atype.AgentCard{Name: "mastra-http", Version: "v1"}, HealthHandler: health}, executor{})
	if err != nil {
		log.Fatal(err)
	}
	log.Fatal(a.Run())
}

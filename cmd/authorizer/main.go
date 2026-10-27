package main

import (
	"context"
	"crypto/tls"
	"example.com/auth-demo/internal/controlplane"
	"example.com/auth-demo/internal/credential"
	"example.com/auth-demo/internal/extproc"
	"example.com/auth-demo/internal/store"
	"example.com/auth-demo/internal/transport"
	extv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	cfg, err := rest.InClusterConfig()
	if err != nil {
		log.Fatal(err)
	}
	k, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		log.Fatal(err)
	}
	db, err := store.Open(os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()
	ns := env("DEMO_NAMESPACE", "demo")
	objects := controlplane.NewWatchedState(k, ns)
	if err = objects.Start(ctx); err != nil {
		log.Fatal(err)
	}
	agent := os.Getenv("AGENT_NAME")
	if agent == "" {
		log.Fatal("AGENT_NAME is required")
	}
	provider, err := credential.NewMounted(ns, agent, os.Getenv("CREDENTIALS_FILE"))
	if err != nil {
		log.Fatal(err)
	}
	s, err := controlplane.New(k, db, env("KAGENT_TARGET", "kagent-controller.kagent.svc:8083"), ns, env("TOKEN_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/token"), objects, provider)
	if err != nil {
		log.Fatal(err)
	}
	s.AgentName = agent
	s.GCInterval = duration("SESSION_GC_INTERVAL", 5*time.Minute)
	s.BindingRetention = duration("BINDING_RETENTION", 24*time.Hour)
	s.RequestRetention = duration("REQUEST_ID_RETENTION", 7*24*time.Hour)
	go s.RunCleanup(ctx)
	httpTLS, err := transport.TLS("/tls", true)
	if err != nil {
		log.Fatal(err)
	}
	grpcTLS, err := transport.TLS("/tls", true)
	if err != nil {
		log.Fatal(err)
	}
	grpcTLS.ClientAuth = tls.RequireAndVerifyClientCert
	processor := grpc.NewServer(grpc.Creds(credentials.NewTLS(grpcTLS)))
	extv3.RegisterExternalProcessorServer(processor, &extproc.Server{Control: s})
	listener, err := net.Listen("tcp", ":50052")
	if err != nil {
		log.Fatal(err)
	}
	go func() {
		if err := processor.Serve(listener); err != nil {
			log.Print(err)
		}
	}()
	server := &http.Server{Addr: ":8444", Handler: s.Handler(), TLSConfig: httpTLS, ReadHeaderTimeout: 10 * time.Second, WriteTimeout: 180 * time.Second}
	go func() {
		<-ctx.Done()
		processor.Stop()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	if err = server.ListenAndServeTLS("", ""); err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
func duration(key string, fallback time.Duration) time.Duration {
	value, err := time.ParseDuration(env(key, fallback.String()))
	if err != nil || value <= 0 {
		log.Fatalf("%s must be a positive duration", key)
	}
	return value
}
func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

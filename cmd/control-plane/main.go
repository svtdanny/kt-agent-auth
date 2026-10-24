package main

import (
	"context"
	"example.com/auth-demo/internal/controlplane"
	"example.com/auth-demo/internal/credential"
	"example.com/auth-demo/internal/store"
	"example.com/auth-demo/internal/transport"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"log"
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
	credentials, err := credential.FromEnv()
	if err != nil {
		log.Fatal(err)
	}
	s, err := controlplane.New(k, db, env("KAGENT_TARGET", "kagent-controller.kagent.svc:8083"), ns, env("TOKEN_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/token"), objects, credentials)
	if err != nil {
		log.Fatal(err)
	}
	s.GCInterval = duration("SESSION_GC_INTERVAL", 5*time.Minute)
	s.BindingRetention = duration("BINDING_RETENTION", 24*time.Hour)
	s.RequestRetention = duration("REQUEST_ID_RETENTION", 7*24*time.Hour)
	go s.RunCleanup(ctx)
	tls, err := transport.TLS("/tls", true)
	if err != nil {
		log.Fatal(err)
	}
	server := &http.Server{Addr: ":8443", Handler: s.Handler(), TLSConfig: tls, ReadHeaderTimeout: 10 * time.Second, WriteTimeout: 180 * time.Second}
	go func() {
		<-ctx.Done()
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

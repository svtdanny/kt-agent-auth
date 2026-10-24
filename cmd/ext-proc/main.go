package main

import (
	"crypto/tls"
	"example.com/auth-demo/internal/controlclient"
	"example.com/auth-demo/internal/extproc"
	"example.com/auth-demo/internal/transport"
	extv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"log"
	"net"
	"net/http"
	"os"
)

func main() {
	client, err := controlclient.New(os.Getenv("CONTROL_PLANE_URL"), "/tls")
	if err != nil {
		log.Fatal(err)
	}
	t, err := transport.TLS("/tls", true)
	if err != nil {
		log.Fatal(err)
	}
	t.ClientAuth = tls.RequireAndVerifyClientCert
	s := grpc.NewServer(grpc.Creds(credentials.NewTLS(t)))
	extv3.RegisterExternalProcessorServer(s, &extproc.Server{Control: client})
	go func() { http.HandleFunc("/readyz", extproc.Health); log.Fatal(http.ListenAndServe(":8080", nil)) }()
	l, err := net.Listen("tcp", ":50051")
	if err != nil {
		log.Fatal(err)
	}
	log.Fatal(s.Serve(l))
}

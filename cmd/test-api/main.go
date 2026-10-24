package main

import (
	"context"
	"encoding/json"
	"example.com/auth-demo/internal/credential"
	"log"
	"net/http"
	"os"
	"sync"
)

func newHandler(readerToken, writerToken string) http.Handler {
	var mu sync.Mutex
	var events []map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/readyz" {
			w.WriteHeader(200)
			return
		}
		if r.URL.Path == "/public" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]bool{"public": true})
			return
		}
		if r.URL.Path == "/audit" {
			mu.Lock()
			defer mu.Unlock()
			_ = json.NewEncoder(w).Encode(events)
			return
		}
		identity := ""
		values := r.Header.Values("Authorization")
		if len(values) == 1 {
			switch {
			case readerToken != "" && values[0] == "Bearer "+readerToken:
				identity = "reader"
			case writerToken != "" && values[0] == "Bearer "+writerToken:
				identity = "writer"
			}
		}
		// Audit key identity and header count, never the credential itself.
		event := map[string]any{"method": r.Method, "path": r.URL.Path, "credential": identity, "credentialAccepted": identity != "", "authorizationHeaderCount": len(values)}
		mu.Lock()
		events = append(events, event)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		if identity == "" {
			w.WriteHeader(401)
		}
		_ = json.NewEncoder(w).Encode(event)
	})
	return mux
}

func main() {
	provider, err := credential.FromEnv()
	if err != nil {
		log.Fatal(err)
	}
	readerRef := os.Getenv("MOCK_READER_REF")
	if readerRef == "" {
		readerRef = "mock/reader"
	}
	writerRef := os.Getenv("MOCK_WRITER_REF")
	if writerRef == "" {
		writerRef = "mock/writer"
	}
	reader, err := provider.Read(context.Background(), "demo", readerRef)
	if err != nil {
		log.Fatal(err)
	}
	writer, err := provider.Read(context.Background(), "demo", writerRef)
	if err != nil {
		log.Fatal(err)
	}
	mux := newHandler(reader, writer)
	if os.Getenv("MOCK_PLAINTEXT") == "true" {
		log.Fatal(http.ListenAndServe(":8080", mux))
	}
	log.Fatal(http.ListenAndServeTLS(":8443", "/tls/tls.crt", "/tls/tls.key", mux))
}

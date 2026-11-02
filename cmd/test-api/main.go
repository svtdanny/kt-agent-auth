package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/google/uuid"
)

func newHandler(readerToken, writerToken string) http.Handler {
	var mu sync.Mutex
	var events []map[string]any
	// Synthetic OAuth fixture: only the most recently issued token remains valid.
	issued := map[string]string{}
	versions := map[string]int{}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/oauth/stats" {
			mu.Lock()
			defer mu.Unlock()
			_ = json.NewEncoder(w).Encode(versions)
			return
		}
		if r.URL.Path == "/oauth/token" {
			user, password, ok := r.BasicAuth()
			_ = r.ParseForm()
			if r.Method != "POST" || !ok || r.Form.Get("grant_type") != "client_credentials" ||
				!((user == "reader" && password == readerToken) || (user == "writer" && password == writerToken)) {
				w.WriteHeader(401)
				return
			}
			mu.Lock()
			defer mu.Unlock()
			issued[user] = uuid.NewString()
			versions[user]++
			_ = json.NewEncoder(w).Encode(map[string]any{"access_token": issued[user], "token_type": "Bearer", "expires_in": 3600})
			return
		}
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
		version := 0
		values := r.Header.Values("Authorization")
		mu.Lock()
		if len(values) == 1 {
			switch {
			case readerToken != "" && values[0] == "Bearer "+readerToken:
				identity = "reader"
			case writerToken != "" && values[0] == "Bearer "+writerToken:
				identity = "writer"
			default:
				for user, token := range issued {
					if values[0] == "Bearer "+token {
						identity = user
						version = versions[user]
					}
				}
			}
		}
		// Audit key identity and header count, never the credential itself.
		event := map[string]any{"method": r.Method, "path": r.URL.Path, "credential": identity, "credentialVersion": version, "credentialAccepted": identity != "", "authorizationHeaderCount": len(values), "remoteAddress": r.RemoteAddr, "timestamp": time.Now().UTC()}
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
	var reader, writer string
	if path := os.Getenv("MOCK_KEYS_FILE"); path != "" {
		// The fixture loads synthetic expected keys from a mounted file.
		// Credential values never appear in its audit output.
		var keys struct {
			Reader string `json:"reader"`
			Writer string `json:"writer"`
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			log.Fatal("cannot read fixture keys")
		}
		if err := json.Unmarshal(raw, &keys); err != nil || keys.Reader == "" || keys.Writer == "" {
			log.Fatal("invalid fixture keys")
		}
		reader, writer = keys.Reader, keys.Writer
	} else {
		log.Fatal("MOCK_KEYS_FILE is required")
	}

	mux := newHandler(reader, writer)
	if os.Getenv("MOCK_PLAINTEXT") == "true" {
		log.Fatal(http.ListenAndServe(":8080", mux))
	}
	log.Fatal(http.ListenAndServeTLS(":8443", "/tls/tls.crt", "/tls/tls.key", mux))
}

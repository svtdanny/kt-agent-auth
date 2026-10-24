package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMockRequiresExactlyOneValidKeyAndNeverEchoesIt(t *testing.T) {
	handler := newHandler("reader-synthetic-key", "writer-synthetic-key")
	for _, tc := range []struct {
		name, identity string
		headers        []string
		status         int
	}{
		{"missing", "", nil, 401},
		{"invalid", "", []string{"Bearer incorrect-key"}, 401},
		{"placeholder", "", []string{"sandbox-placeholder"}, 401},
		{"reader", "reader", []string{"Bearer reader-synthetic-key"}, 200},
		{"writer", "writer", []string{"Bearer writer-synthetic-key"}, 200},
		{"duplicate", "", []string{"Bearer reader-synthetic-key", "Bearer writer-synthetic-key"}, 401},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodGet, "/catalog", nil)
			for _, header := range tc.headers {
				r.Header.Add("Authorization", header)
			}
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, r)
			var event struct {
				Credential               string `json:"credential"`
				AuthorizationHeaderCount int    `json:"authorizationHeaderCount"`
			}
			if err := json.Unmarshal(w.Body.Bytes(), &event); err != nil {
				t.Fatal(err)
			}
			if w.Code != tc.status || event.Credential != tc.identity || event.AuthorizationHeaderCount != len(tc.headers) {
				t.Fatalf("unexpected mock authentication: status=%d event=%+v", w.Code, event)
			}
		})
	}
	r := httptest.NewRequest(http.MethodGet, "/audit", nil)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	for _, key := range []string{"reader-synthetic-key", "writer-synthetic-key", "incorrect-key"} {
		if strings.Contains(w.Body.String(), key) {
			t.Fatal("audit disclosed a credential")
		}
	}
	empty := httptest.NewRecorder()
	r = httptest.NewRequest(http.MethodGet, "/catalog", nil)
	r.Header.Set("Authorization", "Bearer ")
	newHandler("", "").ServeHTTP(empty, r)
	if empty.Code != http.StatusUnauthorized {
		t.Fatal("unconfigured keys accepted an empty bearer credential")
	}
}

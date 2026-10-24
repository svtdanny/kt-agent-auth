package main

import (
	"context"
	"encoding/json"
	"example.com/auth-demo/internal/legacy"
	"example.com/auth-demo/internal/store"
	"log"
	"os"
	"time"
)

func main() {
	s, err := store.Open(os.Getenv("DATABASE_URL"))
	if err != nil {
		log.Fatal(err)
	}
	defer s.Close()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	counts, err := legacy.Import(ctx, s, "/legacy/bindings.db")
	if err != nil {
		log.Fatal(err)
	}
	raw, _ := json.Marshal(counts)
	log.Printf("legacy import complete: %s", raw)
}

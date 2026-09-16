package main

import (
	"embed"
	"io/fs"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/download"
	"github.com/Loccao102/VideoGet/internal/httpapi"
	"github.com/Loccao102/VideoGet/internal/source"
)

//go:embed web/*
var embeddedWeb embed.FS

func main() {
	addr := env("ADDR", ":8080")
	downloadDir := env("DOWNLOAD_DIR", "downloads")
	web, err := fs.Sub(embeddedWeb, "web")
	if err != nil {
		log.Fatalf("load embedded web UI: %v", err)
	}

	// Douyin uses a browser-backed discovery path. Keep a hard outer budget so
	// process cleanup/profile-lock issues can never leave /api/search waiting
	// forever. The HTTP search layer has a 75s source budget; 55s leaves enough
	// time to aggregate and return a structured error to the UI.
	douyin := source.WithSearchTimeout(source.NewDouyinProvider(), 55*time.Second)
	providers := []source.Provider{source.NewBilibiliProvider(), douyin}
	providers = append(providers, source.NewPublicShortProviders()...)
	jobs, err := download.NewManager(downloadDir)
	if err != nil {
		log.Fatalf("initialize job manager: %v", err)
	}
	defer func() {
		if err := jobs.Close(); err != nil {
			log.Printf("close job database: %v", err)
		}
	}()

	api := httpapi.New(providers, jobs, web)
	server := &http.Server{
		Addr:              addr,
		Handler:           api.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      90 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	log.Printf("VideoGet listening on %s", addr)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

func env(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

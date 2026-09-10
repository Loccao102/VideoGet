package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/Loccao102/VideoGet/internal/download"
	"github.com/Loccao102/VideoGet/internal/model"
	"github.com/Loccao102/VideoGet/internal/source"
)

type Server struct {
	providers map[string]source.Provider
	jobs      *download.Manager
	web       fs.FS
}

func New(providers []source.Provider, jobs *download.Manager, web fs.FS) *Server {
	providerMap := map[string]source.Provider{}
	for _, provider := range providers {
		providerMap[provider.Name()] = provider
	}
	return &Server{providers: providerMap, jobs: jobs, web: web}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/health", s.health)
	mux.HandleFunc("POST /api/search", s.search)
	mux.HandleFunc("POST /api/download", s.download)
	mux.HandleFunc("GET /api/jobs", s.listJobs)
	mux.HandleFunc("GET /api/jobs/{id}", s.getJob)
	if s.web != nil {
		mux.Handle("/", http.FileServer(http.FS(s.web)))
	}
	return requestLogger(mux)
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	providers := map[string]map[string]any{}
	for name, provider := range s.providers {
		err := provider.Available()
		providers[name] = map[string]any{"available": err == nil}
		if err != nil {
			providers[name]["error"] = err.Error()
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":       "ok",
		"providers":    providers,
		"localization": s.jobs.LocalizationStatus(),
		"time":         time.Now().UTC(),
	})
}

func (s *Server) search(w http.ResponseWriter, r *http.Request) {
	var req model.SearchRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	req.Keyword = strings.TrimSpace(req.Keyword)
	if req.Keyword == "" {
		writeError(w, http.StatusBadRequest, "keyword is required")
		return
	}
	if req.Limit <= 0 {
		req.Limit = 20
	}
	if req.Limit > 100 {
		req.Limit = 100
	}
	if len(req.Sources) == 0 {
		for name := range s.providers {
			req.Sources = append(req.Sources, name)
		}
		sort.Strings(req.Sources)
	}

	type providerResult struct {
		name   string
		videos []model.Video
		err    error
	}
	results := make(chan providerResult, len(req.Sources))
	var wg sync.WaitGroup
	seen := map[string]struct{}{}

	for _, raw := range req.Sources {
		name := strings.ToLower(strings.TrimSpace(raw))
		if _, ok := seen[name]; ok {
			continue
		}
		seen[name] = struct{}{}
		provider, ok := s.providers[name]
		if !ok {
			results <- providerResult{name: name, err: fmt.Errorf("unknown source")}
			continue
		}
		wg.Add(1)
		go func(name string, provider source.Provider) {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
			defer cancel()
			videos, err := provider.Search(ctx, req.Keyword, req.Limit)
			results <- providerResult{name: name, videos: videos, err: err}
		}(name, provider)
	}
	go func() {
		wg.Wait()
		close(results)
	}()

	response := model.SearchResponse{
		Keyword: req.Keyword,
		Results: []model.Video{},
		Errors:  map[string]string{},
	}
	for result := range results {
		if result.err != nil {
			response.Errors[result.name] = result.err.Error()
		} else {
			response.Results = append(response.Results, result.videos...)
		}
	}
	if len(response.Errors) == 0 {
		response.Errors = nil
	}
	writeJSON(w, http.StatusOK, response)
}

func (s *Server) download(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Video model.Video `json:"video"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	job, err := s.jobs.Start(req.Video)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, job)
}

func (s *Server) getJob(w http.ResponseWriter, r *http.Request) {
	job, ok := s.jobs.Get(r.PathValue("id"))
	if !ok {
		writeError(w, http.StatusNotFound, "job not found")
		return
	}
	writeJSON(w, http.StatusOK, job)
}

func (s *Server) listJobs(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.jobs.List())
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}

func requestLogger(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(start).Round(time.Millisecond))
	})
}

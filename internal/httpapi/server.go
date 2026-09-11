package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Loccao102/VideoGet/internal/discovery"
	"github.com/Loccao102/VideoGet/internal/download"
	"github.com/Loccao102/VideoGet/internal/model"
	"github.com/Loccao102/VideoGet/internal/ranking"
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
	mux.HandleFunc("POST /api/preview", s.preview)
	mux.HandleFunc("POST /api/download", s.download)
	mux.HandleFunc("GET /api/jobs", s.listJobs)
	mux.HandleFunc("GET /api/jobs/{id}", s.getJob)
	mux.HandleFunc("DELETE /api/jobs/{id}", s.deleteJob)
	mux.HandleFunc("POST /api/jobs/{id}/retry", s.retryJob)
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
		"persistence":  s.jobs.PersistenceStatus(),
		"preview": map[string]any{
			"enabled":     true,
			"concurrency": envPositiveInt("PREVIEW_CONCURRENCY", 4),
		},
		"time": time.Now().UTC(),
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
		req.Limit = 30
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
	req.Sources = s.expandFreeSources(req.Sources)

	keywords := []string{req.Keyword}
	var expansionErr error
	if req.Expand {
		keywords, expansionErr = discovery.ExpandContext(r.Context(), req.Keyword)
	}
	if len(keywords) == 0 {
		keywords = []string{req.Keyword}
	}
	perQueryLimit := (req.Limit + len(keywords) - 1) / len(keywords)
	if perQueryLimit < 10 {
		perQueryLimit = 10
	}

	type providerResult struct {
		source  string
		keyword string
		videos  []model.Video
		err     error
	}

	uniqueSources := make([]string, 0, len(req.Sources))
	seenSources := map[string]struct{}{}
	for _, raw := range req.Sources {
		name := strings.ToLower(strings.TrimSpace(raw))
		if name == "" {
			continue
		}
		if _, ok := seenSources[name]; ok {
			continue
		}
		seenSources[name] = struct{}{}
		uniqueSources = append(uniqueSources, name)
	}

	bufferSize := len(uniqueSources) * len(keywords)
	if bufferSize < 1 {
		bufferSize = 1
	}
	results := make(chan providerResult, bufferSize)
	var wg sync.WaitGroup
	for _, name := range uniqueSources {
		provider, ok := s.providers[name]
		if !ok {
			results <- providerResult{source: name, err: fmt.Errorf("unknown source")}
			continue
		}
		sourceKeywords := keywordsForSource(name, keywords)
		for _, keyword := range sourceKeywords {
			keyword := keyword
			wg.Add(1)
			go func(name string, provider source.Provider) {
				defer wg.Done()
				ctx, cancel := context.WithTimeout(r.Context(), 75*time.Second)
				defer cancel()
				videos, err := provider.Search(ctx, keyword, perQueryLimit)
				results <- providerResult{source: name, keyword: keyword, videos: videos, err: err}
			}(name, provider)
		}
	}
	go func() {
		wg.Wait()
		close(results)
	}()

	response := model.SearchResponse{
		Keyword:  req.Keyword,
		Keywords: keywords,
		Results:  []model.Video{},
		Errors:   map[string]string{},
	}
	if expansionErr != nil {
		response.Errors["keyword_expander"] = expansionErr.Error() + "; static fallback was used"
	}

	sourceSuccess := map[string]int{}
	sourceErrors := map[string][]string{}
	for result := range results {
		if result.err != nil {
			message := result.err.Error()
			if result.keyword != "" {
				message = result.keyword + ": " + message
			}
			sourceErrors[result.source] = appendUnique(sourceErrors[result.source], message)
			continue
		}
		if len(result.videos) > 0 {
			sourceSuccess[result.source] += len(result.videos)
			response.Results = append(response.Results, result.videos...)
		}
	}
	for sourceName, errors := range sourceErrors {
		if sourceSuccess[sourceName] > 0 {
			continue
		}
		response.Errors[sourceName] = summarizeErrors(errors)
	}

	response.Results = ranking.Deduplicate(response.Results)
	response.Results = ranking.Rank(response.Results, req.Keyword, req.Sort, req.Filters, time.Now().UTC())
	if len(response.Results) > req.Limit {
		response.Results = response.Results[:req.Limit]
	}
	if len(response.Errors) == 0 {
		response.Errors = nil
	}
	writeJSON(w, http.StatusOK, response)
}

func (s *Server) preview(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Video model.Video `json:"video"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if strings.TrimSpace(req.Video.URL) == "" || strings.TrimSpace(req.Video.Platform) == "" {
		writeError(w, http.StatusBadRequest, "platform and video URL are required")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	video, err := source.EnrichPreview(ctx, req.Video)
	payload := map[string]any{
		"video":     video,
		"available": err == nil && strings.TrimSpace(video.Thumbnail) != "",
	}
	if err != nil {
		payload["error"] = err.Error()
	}
	writeJSON(w, http.StatusOK, payload)
}

func (s *Server) expandFreeSources(requested []string) []string {
	if !envBool("AUTO_FREE_SOURCES", true) {
		return requested
	}
	raw := strings.TrimSpace(os.Getenv("FREE_SHORT_SOURCES"))
	if raw == "" {
		raw = "kuaishou,xiaohongshu,weibo,xigua,haokan,toutiao,acfun,meipai,weishi"
	}
	out := append([]string(nil), requested...)
	seen := map[string]struct{}{}
	for _, name := range out {
		seen[strings.ToLower(strings.TrimSpace(name))] = struct{}{}
	}
	for _, candidate := range strings.Split(raw, ",") {
		name := strings.ToLower(strings.TrimSpace(candidate))
		if name == "" {
			continue
		}
		if _, ok := s.providers[name]; !ok {
			continue
		}
		if _, ok := seen[name]; ok {
			continue
		}
		seen[name] = struct{}{}
		out = append(out, name)
	}
	return out
}

func keywordsForSource(name string, keywords []string) []string {
	if !isPublicShortSource(name) {
		return keywords
	}
	limit := envPositiveInt("PUBLIC_SOURCE_KEYWORD_LIMIT", 3)
	if limit > len(keywords) {
		limit = len(keywords)
	}
	return keywords[:limit]
}

func isPublicShortSource(name string) bool {
	switch name {
	case "kuaishou", "xiaohongshu", "weibo", "xigua", "haokan", "toutiao", "acfun", "meipai", "weishi":
		return true
	default:
		return false
	}
}

func appendUnique(values []string, value string) []string {
	for _, existing := range values {
		if existing == value {
			return values
		}
	}
	return append(values, value)
}

func summarizeErrors(errors []string) string {
	if len(errors) == 0 {
		return "source unavailable"
	}
	max := 2
	if len(errors) < max {
		max = len(errors)
	}
	message := strings.Join(errors[:max], " | ")
	if len(errors) > max {
		message += fmt.Sprintf(" | +%d lỗi tương tự", len(errors)-max)
	}
	return message
}

func envBool(name string, fallback bool) bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv(name)))
	if value == "" {
		return fallback
	}
	return value != "0" && value != "false" && value != "no" && value != "off"
}

func envPositiveInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
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

func (s *Server) retryJob(w http.ResponseWriter, r *http.Request) {
	job, err := s.jobs.Retry(r.PathValue("id"))
	if err != nil {
		status := http.StatusConflict
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, job)
}

func (s *Server) deleteJob(w http.ResponseWriter, r *http.Request) {
	if err := s.jobs.Delete(r.PathValue("id")); err != nil {
		status := http.StatusInternalServerError
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		}
		writeError(w, status, err.Error())
		return
	}
	w.WriteHeader(http.StatusNoContent)
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

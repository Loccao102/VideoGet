package httpapi

import (
	"encoding/json"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/download"
)

type subtitleServer struct {
	jobs *download.Manager
}

// WithSubtitleRoutes wraps the existing API/UI handler so localization-v2 can
// evolve independently from the discovery endpoints.
func WithSubtitleRoutes(next http.Handler, jobs *download.Manager) http.Handler {
	server := &subtitleServer{jobs: jobs}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/jobs/{id}/subtitles", server.getSubtitles)
	mux.HandleFunc("PUT /api/jobs/{id}/subtitles", server.saveSubtitles)
	mux.HandleFunc("POST /api/jobs/{id}/subtitles/rerender", server.rerenderSubtitles)
	mux.HandleFunc("GET /api/jobs/{id}/media/{kind}", server.media)
	mux.Handle("/", next)
	return mux
}

func (s *subtitleServer) getSubtitles(w http.ResponseWriter, r *http.Request) {
	document, err := s.jobs.GetSubtitles(r.PathValue("id"))
	if err != nil {
		status := http.StatusConflict
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, document)
}

func (s *subtitleServer) saveSubtitles(w http.ResponseWriter, r *http.Request) {
	var update download.SubtitleUpdate
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20)).Decode(&update); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	document, err := s.jobs.SaveSubtitles(r.PathValue("id"), update)
	if err != nil {
		status := http.StatusBadRequest
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, document)
}

func (s *subtitleServer) rerenderSubtitles(w http.ResponseWriter, r *http.Request) {
	job, err := s.jobs.RerenderSubtitles(r.PathValue("id"))
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

func (s *subtitleServer) media(w http.ResponseWriter, r *http.Request) {
	job, ok := s.jobs.Get(r.PathValue("id"))
	if !ok {
		writeError(w, http.StatusNotFound, "job not found")
		return
	}
	var path string
	switch strings.ToLower(strings.TrimSpace(r.PathValue("kind"))) {
	case "source", "original":
		path = job.SourceOutput
	case "output", "localized":
		path = job.Output
	default:
		writeError(w, http.StatusBadRequest, "media kind must be source or output")
		return
	}
	if strings.TrimSpace(path) == "" {
		writeError(w, http.StatusNotFound, "media is unavailable")
		return
	}
	file, err := os.Open(path)
	if err != nil {
		writeError(w, http.StatusNotFound, "media is unavailable")
		return
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || info.IsDir() {
		writeError(w, http.StatusNotFound, "media is unavailable")
		return
	}
	if contentType := mime.TypeByExtension(filepath.Ext(path)); contentType != "" {
		w.Header().Set("Content-Type", contentType)
	}
	w.Header().Set("Cache-Control", "no-store")
	http.ServeContent(w, r, filepath.Base(path), info.ModTime().Truncate(time.Second), file)
}

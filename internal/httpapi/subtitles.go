package httpapi

import (
	"encoding/json"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
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
	mux.HandleFunc("GET /api/jobs/{id}/subtitles/quality", server.getSubtitleQuality)
	mux.HandleFunc("GET /api/jobs/{id}/subtitles/context", server.getSubtitleContext)
	mux.HandleFunc("GET /api/jobs/{id}/subtitles/context-overrides", server.getSubtitleContextOverrides)
	mux.HandleFunc("PUT /api/jobs/{id}/subtitles/context-overrides", server.saveSubtitleContextOverrides)
	mux.HandleFunc("PUT /api/jobs/{id}/subtitles", server.saveSubtitles)
	mux.HandleFunc("POST /api/jobs/{id}/subtitles/retranslate", server.retranslateSubtitles)
	mux.HandleFunc("POST /api/jobs/{id}/subtitles/tts-preview", server.previewSubtitleTTS)
	// Explicitly separate output actions: source-audio subtitle render never
	// invokes TTS, while regenerate-tts is the opt-in dubbing path.
	mux.HandleFunc("POST /api/jobs/{id}/subtitles/render", server.renderSubtitlesOnly)
	mux.HandleFunc("POST /api/jobs/{id}/subtitles/regenerate-tts", server.rerenderSubtitles)
	// Backward-compatible alias used by the first V2 editor build. It remains the
	// TTS path; new UI should use /render when source audio must be preserved.
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

func (s *subtitleServer) getSubtitleQuality(w http.ResponseWriter, r *http.Request) {
	document, err := s.jobs.GetSubtitleQuality(r.PathValue("id"))
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

func (s *subtitleServer) getSubtitleContext(w http.ResponseWriter, r *http.Request) {
	document, err := s.jobs.GetSubtitleContext(r.PathValue("id"))
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

func (s *subtitleServer) getSubtitleContextOverrides(w http.ResponseWriter, r *http.Request) {
	document, err := s.jobs.GetSubtitleContextOverrides(r.PathValue("id"))
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

func (s *subtitleServer) saveSubtitleContextOverrides(w http.ResponseWriter, r *http.Request) {
	var update download.SubtitleContextOverrides
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20)).Decode(&update); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	document, err := s.jobs.SaveSubtitleContextOverrides(r.PathValue("id"), update)
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

func (s *subtitleServer) retranslateSubtitles(w http.ResponseWriter, r *http.Request) {
	var request download.SubtitleRetranslateRequest
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	if err := decoder.Decode(&request); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	job, err := s.jobs.RetranslateSubtitles(r.PathValue("id"), request)
	if err != nil {
		status := http.StatusConflict
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		} else if strings.Contains(err.Error(), "unsupported") || strings.Contains(err.Error(), "too long") || strings.Contains(err.Error(), "too many") {
			status = http.StatusBadRequest
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, job)
}

func (s *subtitleServer) previewSubtitleTTS(w http.ResponseWriter, r *http.Request) {
	var request download.SubtitleTTSPreviewRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&request); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	preview, err := s.jobs.PreviewSubtitleTTS(r.PathValue("id"), request)
	if err != nil {
		status := http.StatusBadRequest
		if strings.Contains(err.Error(), "job not found") || strings.Contains(err.Error(), "segment not found") {
			status = http.StatusNotFound
		} else if strings.Contains(err.Error(), "disabled") {
			status = http.StatusConflict
		}
		writeError(w, status, err.Error())
		return
	}
	w.Header().Set("Content-Type", "audio/mpeg")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-VideoGet-TTS-Voice", preview.Voice)
	w.Header().Set("X-VideoGet-TTS-Rate", preview.SpeechRate)
	w.Header().Set("X-VideoGet-TTS-Duration-Ms", strconv.Itoa(preview.DurationMs))
	w.Header().Set("X-VideoGet-TTS-Slot-Ms", strconv.Itoa(preview.SlotMs))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(preview.Audio)
}

func (s *subtitleServer) renderSubtitlesOnly(w http.ResponseWriter, r *http.Request) {
	job, err := s.jobs.RenderSubtitlesOnly(r.PathValue("id"))
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

func (s *subtitleServer) rerenderSubtitles(w http.ResponseWriter, r *http.Request) {
	// This endpoint is explicit TTS opt-in. Persist that choice so the dashboard,
	// retries and subsequent editor actions reflect what the user selected.
	if _, err := s.jobs.SetLocalizationMode(r.PathValue("id"), "subtitles_tts"); err != nil {
		status := http.StatusConflict
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		}
		writeError(w, status, err.Error())
		return
	}
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

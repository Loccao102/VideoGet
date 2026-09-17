package httpapi

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
)

func (s *Server) getSubtitle(w http.ResponseWriter, r *http.Request) {
	content, path, revision, err := s.jobs.Subtitle(r.PathValue("id"))
	if err != nil {
		status := http.StatusConflict
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"content":  content,
		"path":     path,
		"revision": revision,
	})
}

func (s *Server) saveSubtitle(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Content string `json:"content"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	job, err := s.jobs.SaveSubtitle(r.PathValue("id"), req.Content)
	if err != nil {
		status := http.StatusBadRequest
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		} else if strings.Contains(err.Error(), "currently running") {
			status = http.StatusConflict
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, job)
}

func (s *Server) rerenderSubtitle(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Style string `json:"style"`
	}
	if r.Body != nil && r.ContentLength != 0 {
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
		if err := decoder.Decode(&req); err != nil && err != io.EOF {
			writeError(w, http.StatusBadRequest, "invalid JSON body")
			return
		}
	}
	job, err := s.jobs.RerenderSubtitlesWithStyle(r.PathValue("id"), req.Style)
	if err != nil {
		status := http.StatusBadRequest
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		} else if strings.Contains(err.Error(), "currently running") {
			status = http.StatusConflict
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, job)
}

func (s *Server) reprocessOCR(w http.ResponseWriter, r *http.Request) {
	job, err := s.jobs.ReprocessOCR(r.PathValue("id"))
	if err != nil {
		status := http.StatusBadRequest
		if strings.Contains(err.Error(), "not found") {
			status = http.StatusNotFound
		} else if strings.Contains(err.Error(), "currently running") {
			status = http.StatusConflict
		}
		writeError(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusAccepted, job)
}

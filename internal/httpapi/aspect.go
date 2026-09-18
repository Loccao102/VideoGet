package httpapi

import (
	"encoding/json"
	"net/http"
	"strings"
)

func (s *Server) renderAspect(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Aspect string `json:"aspect"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	job, err := s.jobs.RenderAspectVariant(r.PathValue("id"), req.Aspect)
	if err != nil {
		status := http.StatusBadRequest
		message := err.Error()
		if strings.Contains(message, "not found") {
			status = http.StatusNotFound
		} else if strings.Contains(message, "must be complete") {
			status = http.StatusConflict
		}
		writeError(w, status, message)
		return
	}
	writeJSON(w, http.StatusAccepted, job)
}

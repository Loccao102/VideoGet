package download

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/localize"
)

type SubtitleRetranslateRequest struct {
	Profile     string `json:"profile,omitempty"`
	Instruction string `json:"instruction,omitempty"`
	SegmentIDs  []int  `json:"segmentIds,omitempty"`
}

// RetranslateSubtitles starts a background context-aware translation pass. It
// reuses the cached transcript and does not touch the current rendered MP4/TTS.
func (m *Manager) RetranslateSubtitles(id string, request SubtitleRetranslateRequest) (Job, error) {
	id = strings.TrimSpace(id)
	m.mu.Lock()
	job, ok := m.jobs[id]
	if !ok {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job not found")
	}
	if !reusableMedia(job.SourceOutput) {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job source video is unavailable")
	}
	switch job.Status {
	case JobQueued, JobDownloading, JobLocalizing:
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job is already running")
	}
	if len(request.SegmentIDs) > 3000 {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("too many segment ids")
	}
	if len([]rune(strings.TrimSpace(request.Instruction))) > 4000 {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("translation instruction is too long")
	}

	job.Status = JobLocalizing
	job.Error = ""
	job.Attempts++
	job.UpdatedAt = time.Now().UTC()
	m.jobs[id] = job
	m.mu.Unlock()
	if err := m.store.Upsert(job); err != nil {
		return Job{}, err
	}
	go m.runSubtitleRetranslate(id, request)
	return job, nil
}

func (m *Manager) runSubtitleRetranslate(id string, request SubtitleRetranslateRequest) {
	job, ok := m.Get(id)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(envPositiveInt("JOB_TIMEOUT_MINUTES", 180))*time.Minute)
	activeState, active := m.registerActiveJob(id, cancel)
	if !active {
		cancel()
		return
	}
	defer func() {
		cancel()
		m.unregisterActiveJob(id, activeState)
	}()

	if m.localizer == nil || !m.localizer.Enabled() {
		m.fail(id, JobLocalizationFailed, fmt.Errorf("localization is disabled"))
		return
	}
	_, err := m.localizer.RetranslateSubtitles(ctx, job.SourceOutput, localize.RetranslateOptions{
		Profile:     request.Profile,
		Instruction: request.Instruction,
		SegmentIDs:  request.SegmentIDs,
	})
	if err != nil {
		m.fail(id, JobLocalizationFailed, err)
		return
	}
	m.update(id, func(job *Job) {
		job.Status = JobDone
		job.Error = ""
		job.UpdatedAt = time.Now().UTC()
	})
}

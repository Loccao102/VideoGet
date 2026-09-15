package download

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/localize"
)

// SetLocalizationMode updates the persisted per-job preference. It is used when
// the editor explicitly switches between source-audio subtitle rendering and TTS.
func (m *Manager) SetLocalizationMode(id, mode string) (Job, error) {
	id = strings.TrimSpace(id)
	mode = localize.NormalizeMode(mode)
	m.mu.Lock()
	job, ok := m.jobs[id]
	if !ok {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job not found")
	}
	job.Video.LocalizationMode = mode
	job.UpdatedAt = time.Now().UTC()
	m.jobs[id] = job
	m.mu.Unlock()
	if err := m.store.Upsert(job); err != nil {
		return Job{}, err
	}
	return job, nil
}

// RenderSubtitlesOnly renders the current saved subtitle draft while preserving
// the source audio. It never invokes the TTS pipeline.
func (m *Manager) RenderSubtitlesOnly(id string) (Job, error) {
	id = strings.TrimSpace(id)
	m.mu.Lock()
	job, ok := m.jobs[id]
	if !ok {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job not found")
	}
	switch job.Status {
	case JobQueued, JobDownloading, JobLocalizing:
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job is already running")
	}
	if !reusableMedia(job.SourceOutput) {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job source video is unavailable")
	}
	_, _, draftPath, _, _, _, _, _, pathErr := m.subtitlePaths(job)
	if pathErr != nil {
		m.mu.Unlock()
		return Job{}, pathErr
	}
	if _, err := os.Stat(draftPath); err != nil {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("save subtitle edits before rendering")
	}

	job.Video.LocalizationMode = localize.ModeSubtitlesOnly
	job.Status = JobLocalizing
	job.Error = ""
	job.Attempts++
	job.UpdatedAt = time.Now().UTC()
	m.jobs[id] = job
	m.mu.Unlock()
	if err := m.store.Upsert(job); err != nil {
		return Job{}, err
	}
	go m.runSubtitleOnlyRerender(id)
	return job, nil
}

func (m *Manager) runSubtitleOnlyRerender(id string) {
	job, ok := m.Get(id)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(
		context.Background(),
		time.Duration(envPositiveInt("JOB_TIMEOUT_MINUTES", 180))*time.Minute,
	)
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
	result, err := m.localizer.RerenderSubtitlesOnly(ctx, job.SourceOutput)
	if err != nil {
		m.fail(id, JobLocalizationFailed, err)
		return
	}
	m.update(id, func(job *Job) {
		job.Video.LocalizationMode = localize.ModeSubtitlesOnly
		job.Status = JobDone
		job.Localization = &result
		job.Error = ""
		job.Output = result.OutputVideo
		job.UpdatedAt = time.Now().UTC()
	})
}

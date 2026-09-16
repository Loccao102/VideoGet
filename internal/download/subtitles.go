package download

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/localize"
)

var srtTimingPattern = regexp.MustCompile(`(?m)^\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}`)

func (m *Manager) Subtitle(id string) (string, string, int, error) {
	job, ok := m.Get(strings.TrimSpace(id))
	if !ok {
		return "", "", 0, fmt.Errorf("job not found")
	}
	path, err := editableSubtitlePath(job)
	if err != nil {
		return "", "", job.SubtitleRevision, err
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return "", path, job.SubtitleRevision, fmt.Errorf("read subtitle: %w", err)
	}
	return string(content), path, job.SubtitleRevision, nil
}

func (m *Manager) SaveSubtitle(id, content string) (Job, error) {
	id = strings.TrimSpace(id)
	job, ok := m.Get(id)
	if !ok {
		return Job{}, fmt.Errorf("job not found")
	}
	if job.Status == JobQueued || job.Status == JobDownloading || job.Status == JobLocalizing || job.Status == JobRendering {
		return Job{}, fmt.Errorf("job is currently running")
	}
	path, err := editableSubtitlePath(job)
	if err != nil {
		return Job{}, err
	}
	content = normalizeSRT(content)
	if err := validateSRT(content); err != nil {
		return Job{}, err
	}
	if err := ensurePathInsideJob(m.downloadDir, id, path); err != nil {
		return Job{}, err
	}
	if original, readErr := os.ReadFile(path); readErr == nil {
		backup := path + ".bak"
		if _, statErr := os.Stat(backup); os.IsNotExist(statErr) {
			_ = os.WriteFile(backup, original, 0o644)
		}
	}
	temp := path + ".tmp"
	if err := os.WriteFile(temp, []byte(content), 0o644); err != nil {
		return Job{}, fmt.Errorf("write subtitle draft: %w", err)
	}
	if err := os.Rename(temp, path); err != nil {
		_ = os.Remove(temp)
		return Job{}, fmt.Errorf("replace subtitle: %w", err)
	}

	m.update(id, func(current *Job) {
		current.SubtitleRevision++
		current.Error = ""
		current.UpdatedAt = time.Now().UTC()
	})
	updated, _ := m.Get(id)
	return updated, nil
}

func (m *Manager) RerenderSubtitles(id string) (Job, error) {
	id = strings.TrimSpace(id)
	job, ok := m.Get(id)
	if !ok {
		return Job{}, fmt.Errorf("job not found")
	}
	if job.Status == JobQueued || job.Status == JobDownloading || job.Status == JobLocalizing || job.Status == JobRendering {
		return Job{}, fmt.Errorf("job is currently running")
	}
	if !reusableMedia(job.SourceOutput) {
		return Job{}, fmt.Errorf("downloaded source video is unavailable")
	}
	subtitle, err := editableSubtitlePath(job)
	if err != nil {
		return Job{}, err
	}
	if _, err := os.Stat(subtitle); err != nil {
		return Job{}, fmt.Errorf("Vietnamese subtitle is unavailable: %w", err)
	}
	if m.localizer == nil {
		return Job{}, fmt.Errorf("subtitle renderer is unavailable")
	}

	m.update(id, func(current *Job) {
		current.Status = JobRendering
		current.Error = ""
		current.UpdatedAt = time.Now().UTC()
	})
	updated, _ := m.Get(id)
	go m.runSubtitleRender(id)
	return updated, nil
}

func (m *Manager) runSubtitleRender(id string) {
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

	subtitle, err := editableSubtitlePath(job)
	if err != nil {
		m.fail(id, JobLocalizationFailed, err)
		return
	}
	revision := job.SubtitleRevision
	if revision <= 0 {
		revision = 1
	}
	outputDir := filepath.Join(filepath.Dir(job.SourceOutput), "localized")
	output := filepath.Join(outputDir, fmt.Sprintf("%s.vi-subbed-r%d.mp4", strings.TrimSuffix(filepath.Base(job.SourceOutput), filepath.Ext(job.SourceOutput)), revision))
	if err := m.localizer.RenderSubtitles(ctx, job.SourceOutput, subtitle, output); err != nil {
		m.fail(id, JobLocalizationFailed, err)
		return
	}

	m.update(id, func(current *Job) {
		current.Status = JobDone
		current.Error = ""
		current.Output = output
		if current.Localization == nil {
			current.Localization = &localize.Result{VietnameseSubtitle: subtitle}
		}
		current.Localization.VietnameseSubtitle = subtitle
		current.Localization.OutputVideo = output
		current.UpdatedAt = time.Now().UTC()
	})
}

func editableSubtitlePath(job Job) (string, error) {
	if job.Localization == nil || strings.TrimSpace(job.Localization.VietnameseSubtitle) == "" {
		return "", fmt.Errorf("job has no Vietnamese subtitle yet")
	}
	return filepath.Clean(job.Localization.VietnameseSubtitle), nil
}

func ensurePathInsideJob(downloadDir, id, path string) error {
	jobDir, err := filepath.Abs(filepath.Join(downloadDir, id))
	if err != nil {
		return err
	}
	target, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	rel, err := filepath.Rel(jobDir, target)
	if err != nil {
		return err
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return fmt.Errorf("subtitle path is outside the job directory")
	}
	return nil
}

func normalizeSRT(content string) string {
	content = strings.TrimPrefix(content, "\ufeff")
	content = strings.ReplaceAll(content, "\r\n", "\n")
	content = strings.ReplaceAll(content, "\r", "\n")
	return strings.TrimSpace(content) + "\n"
}

func validateSRT(content string) error {
	if strings.TrimSpace(content) == "" {
		return fmt.Errorf("subtitle cannot be empty")
	}
	if len(content) > 2<<20 {
		return fmt.Errorf("subtitle exceeds 2 MiB limit")
	}
	if !srtTimingPattern.MatchString(content) {
		return fmt.Errorf("invalid SRT: no HH:MM:SS,mmm --> HH:MM:SS,mmm timing line found")
	}
	return nil
}

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

const (
	SubtitleRenderStandard   = "standard"
	SubtitleRenderOCROverlay = "ocr_overlay" // backwards-compatible alias for clean
	SubtitleRenderOCRClean   = "ocr_clean"
	SubtitleRenderOCRCapsule = "ocr_capsule"
	SubtitleRenderOCRBox     = "ocr_box"
)

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
	if job.Status == JobQueued || job.Status == JobDownloading || job.Status == JobLocalizing || job.Status == JobRendering || job.Status == JobAspectRendering {
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

// ReprocessOCR reruns the OCR pipeline against the already downloaded source.
// It intentionally keeps SourceOutput so no network download is repeated.
func (m *Manager) ReprocessOCR(id string) (Job, error) {
	id = strings.TrimSpace(id)
	job, ok := m.Get(id)
	if !ok {
		return Job{}, fmt.Errorf("job not found")
	}
	if job.Status == JobQueued || job.Status == JobDownloading || job.Status == JobLocalizing || job.Status == JobRendering || job.Status == JobAspectRendering {
		return Job{}, fmt.Errorf("job is currently running")
	}
	if job.ProcessingMode != ProcessingOCRSubtitles && job.ProcessingMode != ProcessingOCRMusic {
		return Job{}, fmt.Errorf("OCR reprocess is only available for OCR jobs")
	}
	if !reusableMedia(job.SourceOutput) {
		return Job{}, fmt.Errorf("downloaded source video is unavailable")
	}

	m.update(id, func(current *Job) {
		current.Status = JobQueued
		current.Error = ""
		current.RenderedOutput = ""
		current.AspectOutputs = nil
		current.Output = current.SourceOutput
		current.Localization = nil
		current.SubtitleRevision = 0
		current.Attempts++
		current.UpdatedAt = time.Now().UTC()
	})
	updated, _ := m.Get(id)
	go m.run(id)
	return updated, nil
}

func (m *Manager) RerenderSubtitles(id string) (Job, error) {
	return m.RerenderSubtitlesWithStyle(id, SubtitleRenderStandard)
}

func normalizeOCRRenderStyle(style string) (string, bool) {
	switch style {
	case SubtitleRenderOCROverlay, SubtitleRenderOCRClean:
		return "clean", true
	case SubtitleRenderOCRCapsule:
		return "capsule", true
	case SubtitleRenderOCRBox:
		return "box", true
	default:
		return "", false
	}
}

func (m *Manager) RerenderSubtitlesWithStyle(id, style string) (Job, error) {
	id = strings.TrimSpace(id)
	style = strings.ToLower(strings.TrimSpace(style))
	if style == "" {
		style = SubtitleRenderStandard
	}
	overlayStyle, isOCRStyle := normalizeOCRRenderStyle(style)
	if style != SubtitleRenderStandard && !isOCRStyle {
		return Job{}, fmt.Errorf("unknown subtitle render style %q", style)
	}

	job, ok := m.Get(id)
	if !ok {
		return Job{}, fmt.Errorf("job not found")
	}
	if job.Status == JobQueued || job.Status == JobDownloading || job.Status == JobLocalizing || job.Status == JobRendering || job.Status == JobAspectRendering {
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
	if isOCRStyle {
		if job.ProcessingMode != ProcessingOCRSubtitles && job.ProcessingMode != ProcessingOCRMusic {
			return Job{}, fmt.Errorf("OCR overlay render is only available for OCR jobs")
		}
		if _, err := ocrMetadataPath(job); err != nil {
			return Job{}, err
		}
	}

	m.update(id, func(current *Job) {
		current.Status = JobRendering
		current.Error = ""
		current.UpdatedAt = time.Now().UTC()
	})
	updated, _ := m.Get(id)
	go m.runSubtitleRender(id, style, overlayStyle)
	return updated, nil
}

func (m *Manager) runSubtitleRender(id, style, overlayStyle string) {
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
	stem := strings.TrimSuffix(filepath.Base(job.SourceOutput), filepath.Ext(job.SourceOutput))
	var output string

	if overlayStyle != "" {
		metadata, metaErr := ocrMetadataPath(job)
		if metaErr != nil {
			m.fail(id, JobLocalizationFailed, metaErr)
			return
		}
		output = filepath.Join(outputDir, fmt.Sprintf("%s.ocr-%s-r%d.mp4", stem, overlayStyle, revision))
		err = m.localizer.RenderOCROverlaySubtitles(
			ctx,
			job.SourceOutput,
			subtitle,
			metadata,
			output,
			job.Video.Platform,
			overlayStyle,
		)
	} else if job.ProcessingMode == ProcessingOCRMusic {
		output = filepath.Join(outputDir, fmt.Sprintf("%s.ocr-music-r%d.mp4", stem, revision))
		err = m.localizer.RenderOCRMusicSubtitles(ctx, job.SourceOutput, subtitle, output)
	} else {
		output = filepath.Join(outputDir, fmt.Sprintf("%s.vi-subbed-r%d.mp4", stem, revision))
		err = m.localizer.RenderSubtitles(ctx, job.SourceOutput, subtitle, output)
	}
	if err != nil {
		m.fail(id, JobLocalizationFailed, err)
		return
	}

	finalOutput := output
	if job.OutputAspect != OutputAspectOriginal {
		converted, convertErr := m.applyOutputAspect(ctx, output, job.OutputAspect)
		if convertErr != nil {
			m.fail(id, JobLocalizationFailed, convertErr)
			return
		}
		finalOutput = converted
	}

	m.update(id, func(current *Job) {
		current.Status = JobDone
		current.Error = ""
		current.RenderedOutput = output
		current.Output = finalOutput
		current.AspectOutputs = map[string]string{OutputAspectOriginal: output}
		if current.OutputAspect != OutputAspectOriginal {
			current.AspectOutputs[current.OutputAspect] = finalOutput
		}
		if current.Localization == nil {
			current.Localization = &localize.Result{VietnameseSubtitle: subtitle}
		}
		current.Localization.VietnameseSubtitle = subtitle
		current.Localization.OutputVideo = finalOutput
		current.UpdatedAt = time.Now().UTC()
	})
}

func editableSubtitlePath(job Job) (string, error) {
	if job.Localization == nil || strings.TrimSpace(job.Localization.VietnameseSubtitle) == "" {
		return "", fmt.Errorf("job has no Vietnamese subtitle yet")
	}
	return filepath.Clean(job.Localization.VietnameseSubtitle), nil
}

func ocrMetadataPath(job Job) (string, error) {
	if strings.TrimSpace(job.SourceOutput) == "" {
		return "", fmt.Errorf("OCR source video is unavailable")
	}
	outputDir := filepath.Join(filepath.Dir(job.SourceOutput), "localized")
	stem := strings.TrimSuffix(filepath.Base(job.SourceOutput), filepath.Ext(job.SourceOutput))
	candidates := []string{
		filepath.Join(outputDir, stem+".ocr-subtitles.json"),
		filepath.Join(outputDir, stem+".ocr-music.json"),
	}
	for _, path := range candidates {
		info, err := os.Stat(path)
		if err == nil && !info.IsDir() && info.Size() > 0 {
			return path, nil
		}
	}
	return "", fmt.Errorf("OCR metadata is unavailable; run OCR again first")
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

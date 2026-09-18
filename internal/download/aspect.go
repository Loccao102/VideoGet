package download

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	OutputAspectOriginal = "original"
	OutputAspect16x9      = "16:9"
	OutputAspect3x4       = "3:4"
	OutputAspect9x16      = "9:16"
	OutputAspect1x1       = "1:1"
)

func validateOutputAspect(value string) (string, error) {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		value = OutputAspectOriginal
	}
	switch value {
	case OutputAspectOriginal, OutputAspect16x9, OutputAspect3x4, OutputAspect9x16, OutputAspect1x1:
		return value, nil
	default:
		return "", fmt.Errorf("output aspect must be original, 16:9, 3:4, 9:16, or 1:1")
	}
}

func normalizeOutputAspect(value string) string {
	aspect, err := validateOutputAspect(value)
	if err != nil {
		return OutputAspectOriginal
	}
	return aspect
}

func aspectSuffix(aspect string) string {
	return strings.NewReplacer(":", "x", "/", "x", " ", "").Replace(normalizeOutputAspect(aspect))
}

func aspectOutputPath(input, aspect string) string {
	ext := filepath.Ext(input)
	stem := strings.TrimSuffix(filepath.Base(input), ext)
	return filepath.Join(filepath.Dir(input), fmt.Sprintf("%s.aspect-%s.mp4", stem, aspectSuffix(aspect)))
}

func (m *Manager) applyOutputAspect(ctx context.Context, input, aspect string) (string, error) {
	aspect = normalizeOutputAspect(aspect)
	if aspect == OutputAspectOriginal {
		return input, nil
	}
	if m.localizer == nil {
		return "", fmt.Errorf("aspect converter is unavailable")
	}
	output := aspectOutputPath(input, aspect)
	if err := m.localizer.ConvertAspect(ctx, input, output, aspect); err != nil {
		return "", err
	}
	return output, nil
}


func aspectBaseFromDerivative(path string) string {
	path = strings.TrimSpace(path)
	if path == "" {
		return ""
	}
	lower := strings.ToLower(path)
	index := strings.LastIndex(lower, ".aspect-")
	if index < 0 || !strings.HasSuffix(lower, ".mp4") {
		return ""
	}
	candidate := path[:index] + ".mp4"
	if reusableMedia(candidate) {
		return candidate
	}
	return ""
}

func completedAspectBase(job Job) string {
	if reusableMedia(job.RenderedOutput) {
		return job.RenderedOutput
	}
	if job.ProcessingMode == ProcessingDownload && reusableMedia(job.SourceOutput) {
		return job.SourceOutput
	}
	if candidate := aspectBaseFromDerivative(job.Output); candidate != "" {
		return candidate
	}
	if job.Localization != nil {
		if candidate := aspectBaseFromDerivative(job.Localization.OutputVideo); candidate != "" {
			return candidate
		}
	}
	if reusableMedia(job.Output) {
		return job.Output
	}
	return ""
}

// RenderAspectVariant creates another aspect-ratio version from an already
// completed job without rerunning OCR, translation, subtitles or TTS.
func (m *Manager) RenderAspectVariant(id, aspect string) (Job, error) {
	id = strings.TrimSpace(id)
	aspect, err := validateOutputAspect(aspect)
	if err != nil {
		return Job{}, err
	}
	job, ok := m.Get(id)
	if !ok {
		return Job{}, fmt.Errorf("job not found")
	}
	if job.Status != JobDone {
		return Job{}, fmt.Errorf("job must be complete before rendering another aspect ratio")
	}
	base := completedAspectBase(job)
	if base == "" {
		return Job{}, fmt.Errorf("completed rendered video is unavailable")
	}
	if existing := strings.TrimSpace(job.AspectOutputs[aspect]); existing != "" && reusableMedia(existing) {
		return job, nil
	}

	m.update(id, func(current *Job) {
		current.Status = JobRendering
		current.Error = ""
		current.UpdatedAt = time.Now().UTC()
	})
	updated, _ := m.Get(id)
	go m.runAspectVariant(id, aspect, base)
	return updated, nil
}

func (m *Manager) runAspectVariant(id, aspect, base string) {
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

	output := base
	var err error
	if aspect != OutputAspectOriginal {
		output, err = m.applyOutputAspect(ctx, base, aspect)
	}
	if err != nil {
		m.update(id, func(current *Job) {
			current.Status = JobDone
			current.Error = fmt.Sprintf("aspect %s render failed: %v", aspect, err)
			current.UpdatedAt = time.Now().UTC()
		})
		return
	}
	if info, statErr := os.Stat(output); statErr != nil || info.IsDir() || info.Size() <= 0 {
		m.update(id, func(current *Job) {
			current.Status = JobDone
			current.Error = fmt.Sprintf("aspect %s render produced no usable video", aspect)
			current.UpdatedAt = time.Now().UTC()
		})
		return
	}

	m.update(id, func(current *Job) {
		current.Status = JobDone
		current.Error = ""
		if current.RenderedOutput == "" {
			current.RenderedOutput = base
		}
		if current.AspectOutputs == nil {
			current.AspectOutputs = map[string]string{}
		}
		current.AspectOutputs[OutputAspectOriginal] = base
		current.AspectOutputs[aspect] = output
		current.UpdatedAt = time.Now().UTC()
	})
}

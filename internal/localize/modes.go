package localize

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const (
	ModeSubtitles = "subtitles"
	ModeDub       = "dub"
)

// ProcessMode keeps the existing persistent worker for full dubbing jobs and
// uses a dedicated one-shot script for subtitle-only jobs so TTS is never invoked.
func (p *Processor) ProcessMode(ctx context.Context, input, mode string) (Result, error) {
	mode = strings.ToLower(strings.TrimSpace(mode))
	if mode == "" || mode == ModeDub {
		return p.Process(ctx, input)
	}
	if mode != ModeSubtitles {
		return Result{}, fmt.Errorf("unsupported localization mode %q", mode)
	}
	if !p.Enabled() {
		return Result{OutputVideo: input}, nil
	}
	if _, err := exec.LookPath(p.python); err != nil {
		return Result{}, fmt.Errorf("python runtime not found: %w", err)
	}
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		return Result{}, fmt.Errorf("ffmpeg not found: %w", err)
	}
	if strings.TrimSpace(input) == "" {
		return Result{}, fmt.Errorf("input video path is required")
	}
	if _, err := os.Stat(input); err != nil {
		return Result{}, fmt.Errorf("input video does not exist: %w", err)
	}

	script := strings.TrimSpace(os.Getenv("SUBTITLE_LOCALIZE_SCRIPT"))
	if script == "" {
		script = firstExisting(
			"/app/scripts/localize_subtitles.py",
			filepath.FromSlash("scripts/localize_subtitles.py"),
		)
	}
	if script == "" {
		return Result{}, fmt.Errorf("subtitle localization script not found")
	}

	select {
	case p.sem <- struct{}{}:
		defer func() { <-p.sem }()
	case <-ctx.Done():
		return Result{}, ctx.Err()
	}

	outputDir := filepath.Join(filepath.Dir(input), "localized")
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return Result{}, fmt.Errorf("create localization output directory: %w", err)
	}

	cmd := exec.CommandContext(ctx, p.python, script,
		"--input", input,
		"--output-dir", outputDir,
	)
	cmd.Env = os.Environ()
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return Result{}, fmt.Errorf("subtitle localization failed: %s", message)
	}
	var result Result
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &result); err != nil {
		return Result{}, fmt.Errorf("decode subtitle localization result: %w; output=%q", err, strings.TrimSpace(stdout.String()))
	}
	return validateResult(result)
}

// RenderSubtitles burns the currently edited Vietnamese SRT into the already
// downloaded source video. It intentionally does not transcribe, translate or TTS.
func (p *Processor) RenderSubtitles(ctx context.Context, input, subtitle, output string) error {
	if strings.TrimSpace(input) == "" || strings.TrimSpace(subtitle) == "" || strings.TrimSpace(output) == "" {
		return fmt.Errorf("input, subtitle and output paths are required")
	}
	for _, path := range []string{input, subtitle} {
		if _, err := os.Stat(path); err != nil {
			return fmt.Errorf("required file %s is unavailable: %w", path, err)
		}
	}
	python := p.python
	if strings.TrimSpace(python) == "" {
		python = "python3"
	}
	script := strings.TrimSpace(os.Getenv("SUBTITLE_RENDER_SCRIPT"))
	if script == "" {
		script = firstExisting(
			"/app/scripts/render_subtitles.py",
			filepath.FromSlash("scripts/render_subtitles.py"),
		)
	}
	if script == "" {
		return fmt.Errorf("subtitle render script not found")
	}

	select {
	case p.sem <- struct{}{}:
		defer func() { <-p.sem }()
	case <-ctx.Done():
		return ctx.Err()
	}

	if err := os.MkdirAll(filepath.Dir(output), 0o755); err != nil {
		return fmt.Errorf("create subtitle render directory: %w", err)
	}
	cmd := exec.CommandContext(ctx, python, script,
		"--input", input,
		"--subtitle", subtitle,
		"--output", output,
	)
	cmd.Env = os.Environ()
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return fmt.Errorf("subtitle render failed: %s", message)
	}
	if info, err := os.Stat(output); err != nil || info.IsDir() || info.Size() <= 0 {
		if err == nil {
			err = fmt.Errorf("empty output")
		}
		return fmt.Errorf("subtitle render produced no usable video: %w", err)
	}
	return nil
}

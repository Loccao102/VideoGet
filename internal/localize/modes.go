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
	ModeSubtitles    = "subtitles"
	ModeDub          = "dub"
	ModeOCRMusic     = "ocr_music"
	ModeOCRSubtitles = "ocr_subtitles"
)

// ProcessMode keeps backwards compatibility for callers that do not request
// an output aspect during the localization render.
func (p *Processor) ProcessMode(ctx context.Context, input, mode string) (Result, error) {
	return p.ProcessModeWithAspect(ctx, input, mode, "original")
}

// ProcessModeWithAspect lets OCR modes burn cleanup/subtitles and apply the final
// social aspect in the same video encode. Non-OCR modes keep their legacy flow.
func (p *Processor) ProcessModeWithAspect(ctx context.Context, input, mode, aspect string) (Result, error) {
	mode = strings.ToLower(strings.TrimSpace(mode))
	aspect = strings.ToLower(strings.TrimSpace(aspect))
	if aspect == "" {
		aspect = "original"
	}
	if mode == "" || mode == ModeDub {
		return p.Process(ctx, input)
	}

	var script string
	var label string
	switch mode {
	case ModeSubtitles:
		script = strings.TrimSpace(os.Getenv("SUBTITLE_LOCALIZE_SCRIPT"))
		if script == "" {
			script = firstExisting(
				"/app/scripts/localize_subtitles.py",
				filepath.FromSlash("scripts/localize_subtitles.py"),
			)
		}
		label = "subtitle localization"
	case ModeOCRSubtitles:
		script = strings.TrimSpace(os.Getenv("OCR_SUBTITLE_SCRIPT"))
		if script == "" {
			script = firstExisting(
				"/app/scripts/localize_ocr_subtitles.py",
				filepath.FromSlash("scripts/localize_ocr_subtitles.py"),
			)
		}
		label = "OCR subtitle localization"
	case ModeOCRMusic:
		script = strings.TrimSpace(os.Getenv("OCR_MUSIC_SCRIPT"))
		if script == "" {
			script = firstExisting(
				"/app/scripts/localize_ocr_music.py",
				filepath.FromSlash("scripts/localize_ocr_music.py"),
			)
		}
		label = "OCR + music localization"
	default:
		return Result{}, fmt.Errorf("unsupported localization mode %q", mode)
	}
	if script == "" {
		return Result{}, fmt.Errorf("%s script not found", label)
	}
	extraArgs := []string{}
	if mode == ModeOCRSubtitles || mode == ModeOCRMusic {
		extraArgs = append(extraArgs, "--aspect", aspect)
	}
	return p.processOneShotMode(ctx, input, script, label, extraArgs...)
}

func (p *Processor) processOneShotMode(ctx context.Context, input, script, label string, extraArgs ...string) (Result, error) {
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

	args := []string{
		script,
		"--input", input,
		"--output-dir", outputDir,
	}
	args = append(args, extraArgs...)
	cmd := exec.CommandContext(ctx, p.python, args...)
	cmd.Env = os.Environ()
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return Result{}, fmt.Errorf("%s failed: %s", label, message)
	}
	var result Result
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &result); err != nil {
		return Result{}, fmt.Errorf("decode %s result: %w; output=%q", label, err, strings.TrimSpace(stdout.String()))
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

// RenderOCROverlaySubtitles reuses the OCR footprint stored in metadata and only
// changes presentation. It never runs OCR/translation/Whisper/TTS again.
func (p *Processor) RenderOCROverlaySubtitles(ctx context.Context, input, subtitle, metadata, output, platform, style string) error {
	return p.RenderOCROverlaySubtitlesWithAspect(ctx, input, subtitle, metadata, output, platform, style, "original")
}

func (p *Processor) RenderOCROverlaySubtitlesWithAspect(ctx context.Context, input, subtitle, metadata, output, platform, style, aspect string) error {
	aspect = strings.ToLower(strings.TrimSpace(aspect))
	if aspect == "" {
		aspect = "original"
	}
	if strings.TrimSpace(input) == "" || strings.TrimSpace(subtitle) == "" || strings.TrimSpace(metadata) == "" || strings.TrimSpace(output) == "" {
		return fmt.Errorf("input, subtitle, OCR metadata and output paths are required")
	}
	for _, path := range []string{input, subtitle, metadata} {
		if _, err := os.Stat(path); err != nil {
			return fmt.Errorf("required file %s is unavailable: %w", path, err)
		}
	}
	python := p.python
	if strings.TrimSpace(python) == "" {
		python = "python3"
	}
	script := strings.TrimSpace(os.Getenv("OCR_OVERLAY_RENDER_SCRIPT"))
	if script == "" {
		script = firstExisting(
			"/app/scripts/render_ocr_overlay.py",
			filepath.FromSlash("scripts/render_ocr_overlay.py"),
		)
	}
	if script == "" {
		return fmt.Errorf("OCR overlay render script not found")
	}

	select {
	case p.sem <- struct{}{}:
		defer func() { <-p.sem }()
	case <-ctx.Done():
		return ctx.Err()
	}

	if err := os.MkdirAll(filepath.Dir(output), 0o755); err != nil {
		return fmt.Errorf("create OCR overlay render directory: %w", err)
	}
	cmd := exec.CommandContext(ctx, python, script,
		"--input", input,
		"--subtitle", subtitle,
		"--metadata", metadata,
		"--output", output,
		"--platform", strings.TrimSpace(platform),
		"--style", strings.TrimSpace(style),
		"--aspect", aspect,
	)
	cmd.Env = os.Environ()
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return fmt.Errorf("OCR overlay render failed: %s", message)
	}
	if info, err := os.Stat(output); err != nil || info.IsDir() || info.Size() <= 0 {
		if err == nil {
			err = fmt.Errorf("empty output")
		}
		return fmt.Errorf("OCR overlay render produced no usable video: %w", err)
	}
	return nil
}

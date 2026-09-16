package localize

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// RenderOCRMusicSubtitles burns an edited OCR-generated Vietnamese SRT and
// reconstructs the same music mix without rerunning OCR, translation, Whisper or TTS.
func (p *Processor) RenderOCRMusicSubtitles(ctx context.Context, input, subtitle, output string) error {
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
	script := strings.TrimSpace(os.Getenv("OCR_MUSIC_RERENDER_SCRIPT"))
	if script == "" {
		script = firstExisting(
			"/app/scripts/rerender_ocr_music.py",
			filepath.FromSlash("scripts/rerender_ocr_music.py"),
		)
	}
	if script == "" {
		return fmt.Errorf("OCR music re-render script not found")
	}

	select {
	case p.sem <- struct{}{}:
		defer func() { <-p.sem }()
	case <-ctx.Done():
		return ctx.Err()
	}

	if err := os.MkdirAll(filepath.Dir(output), 0o755); err != nil {
		return fmt.Errorf("create OCR music render directory: %w", err)
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
		return fmt.Errorf("OCR music re-render failed: %s", message)
	}
	if info, err := os.Stat(output); err != nil || info.IsDir() || info.Size() <= 0 {
		if err == nil {
			err = fmt.Errorf("empty output")
		}
		return fmt.Errorf("OCR music re-render produced no usable video: %w", err)
	}
	return nil
}

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

// RerenderSubtitlesOnly skips Whisper/translation/TTS and regenerates only the
// subtitle-burned video from a saved edit draft while preserving source audio.
func (p *Processor) RerenderSubtitlesOnly(ctx context.Context, input string) (Result, error) {
	if p == nil || !p.Enabled() {
		return Result{}, fmt.Errorf("localization is disabled")
	}
	input = strings.TrimSpace(input)
	if input == "" {
		return Result{}, fmt.Errorf("input video path is required")
	}
	if _, err := os.Stat(input); err != nil {
		return Result{}, fmt.Errorf("input video does not exist: %w", err)
	}
	if _, err := exec.LookPath(p.python); err != nil {
		return Result{}, fmt.Errorf("python runtime not found: %w", err)
	}

	script := strings.TrimSpace(os.Getenv("SUBTITLE_ONLY_RERENDER_SCRIPT"))
	if script == "" {
		script = firstExisting(
			"/app/scripts/rerender_subtitles_only.py",
			filepath.FromSlash("scripts/rerender_subtitles_only.py"),
		)
	}
	if script == "" {
		return Result{}, fmt.Errorf("subtitle-only rerender script was not found")
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
		return Result{}, fmt.Errorf("subtitle-only rerender failed: %s", message)
	}

	var result Result
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &result); err != nil {
		return Result{}, fmt.Errorf("decode subtitle-only rerender result: %w; output=%q", err, strings.TrimSpace(stdout.String()))
	}
	result.LocalizationMode = ModeSubtitlesOnly
	return validateResult(result)
}

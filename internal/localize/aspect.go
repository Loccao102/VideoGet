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

// ConvertAspect creates a social-aspect derivative without modifying the source.
func (p *Processor) ConvertAspect(ctx context.Context, input, output, aspect string) error {
	if strings.TrimSpace(input) == "" || strings.TrimSpace(output) == "" || strings.TrimSpace(aspect) == "" {
		return fmt.Errorf("input, output and aspect are required")
	}
	if _, err := os.Stat(input); err != nil {
		return fmt.Errorf("aspect source video is unavailable: %w", err)
	}
	python := p.python
	if strings.TrimSpace(python) == "" {
		python = "python3"
	}
	script := strings.TrimSpace(os.Getenv("ASPECT_CONVERT_SCRIPT"))
	if script == "" {
		script = firstExisting(
			"/app/scripts/convert_aspect.py",
			filepath.FromSlash("scripts/convert_aspect.py"),
		)
	}
	if script == "" {
		return fmt.Errorf("aspect conversion script not found")
	}

	select {
	case p.sem <- struct{}{}:
		defer func() { <-p.sem }()
	case <-ctx.Done():
		return ctx.Err()
	}

	if err := os.MkdirAll(filepath.Dir(output), 0o755); err != nil {
		return fmt.Errorf("create aspect output directory: %w", err)
	}
	cmd := exec.CommandContext(ctx, python, script,
		"--input", input,
		"--output", output,
		"--aspect", strings.TrimSpace(aspect),
	)
	cmd.Env = os.Environ()
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = strings.TrimSpace(stdout.String())
		}
		if message == "" {
			message = err.Error()
		}
		return fmt.Errorf("aspect conversion failed: %s", message)
	}
	if info, err := os.Stat(output); err != nil || info.IsDir() || info.Size() <= 0 {
		if err == nil {
			err = fmt.Errorf("empty output")
		}
		return fmt.Errorf("aspect conversion produced no usable video: %w", err)
	}
	return nil
}

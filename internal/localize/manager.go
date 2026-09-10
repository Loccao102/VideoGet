package localize

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
)

type Result struct {
	OriginalSubtitle   string `json:"originalSubtitle,omitempty"`
	VietnameseSubtitle string `json:"vietnameseSubtitle,omitempty"`
	VoiceTrack         string `json:"voiceTrack,omitempty"`
	OutputVideo        string `json:"outputVideo,omitempty"`
	DetectedLanguage   string `json:"detectedLanguage,omitempty"`
	Segments           int    `json:"segments,omitempty"`
	SkippedReason      string `json:"skippedReason,omitempty"`
}

type Processor struct {
	enabled bool
	python  string
	script  string
	sem     chan struct{}
}

func NewFromEnv() *Processor {
	enabled := true
	if value := strings.TrimSpace(os.Getenv("AUTO_LOCALIZE")); value != "" {
		switch strings.ToLower(value) {
		case "0", "false", "no", "off":
			enabled = false
		}
	}

	python := strings.TrimSpace(os.Getenv("PYTHON_BIN"))
	if python == "" {
		python = "python3"
	}

	script := strings.TrimSpace(os.Getenv("LOCALIZE_SCRIPT"))
	if script == "" {
		candidates := []string{
			"/app/scripts/localize_fast.py",
			"/app/scripts/localize.py",
			filepath.FromSlash("scripts/localize_fast.py"),
			filepath.FromSlash("scripts/localize.py"),
		}
		for _, candidate := range candidates {
			if _, err := os.Stat(candidate); err == nil {
				script = candidate
				break
			}
		}
		if script == "" {
			script = filepath.FromSlash("scripts/localize.py")
		}
	}

	concurrency := 1
	if raw := strings.TrimSpace(os.Getenv("LOCALIZE_CONCURRENCY")); raw != "" {
		if parsed, err := strconv.Atoi(raw); err == nil && parsed > 0 {
			concurrency = parsed
		}
	}

	return &Processor{
		enabled: enabled,
		python:  python,
		script:  script,
		sem:     make(chan struct{}, concurrency),
	}
}

func (p *Processor) Enabled() bool { return p != nil && p.enabled }

func (p *Processor) Available() error {
	if !p.Enabled() {
		return nil
	}
	if _, err := exec.LookPath(p.python); err != nil {
		return fmt.Errorf("python runtime not found: %w", err)
	}
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		return fmt.Errorf("ffmpeg not found: %w", err)
	}
	if _, err := exec.LookPath("ffprobe"); err != nil {
		return fmt.Errorf("ffprobe not found: %w", err)
	}
	if _, err := os.Stat(p.script); err != nil {
		return fmt.Errorf("localization script not found at %s: %w", p.script, err)
	}
	return nil
}

func (p *Processor) Process(ctx context.Context, input string) (Result, error) {
	if !p.Enabled() {
		return Result{OutputVideo: input}, nil
	}
	if err := p.Available(); err != nil {
		return Result{}, err
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

	cmd := exec.CommandContext(ctx, p.python, p.script,
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
		return Result{}, fmt.Errorf("localization failed: %s", message)
	}

	var result Result
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &result); err != nil {
		return Result{}, fmt.Errorf("decode localization result: %w; output=%q", err, strings.TrimSpace(stdout.String()))
	}
	if result.OutputVideo == "" {
		return Result{}, fmt.Errorf("localization finished without output video")
	}
	return result, nil
}

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

type RetranslateOptions struct {
	Profile     string `json:"profile,omitempty"`
	Instruction string `json:"instruction,omitempty"`
	SegmentIDs  []int  `json:"segmentIds,omitempty"`
}

type RetranslateResult struct {
	Segments               int            `json:"segments"`
	TargetSegments         any            `json:"targetSegments,omitempty"`
	Profile                string         `json:"profile,omitempty"`
	TranslatedFile         string         `json:"translatedFile,omitempty"`
	DraftFile              string         `json:"draftFile,omitempty"`
	VietnameseSubtitle     string         `json:"vietnameseSubtitle,omitempty"`
	TranslationContextFile string         `json:"translationContextFile,omitempty"`
	TranslationQAFile      string         `json:"translationQAFile,omitempty"`
	TranslationQA          map[string]any `json:"translationQA,omitempty"`
	SemanticBlocks         int            `json:"semanticBlocks,omitempty"`
	ElapsedSeconds         float64        `json:"elapsedSeconds,omitempty"`
}

// RetranslateSubtitles reuses the cached source transcript and runs only the
// Localization V2 context-analysis + translation stages. It deliberately leaves
// the current rendered MP4 untouched until the user explicitly regenerates TTS.
func (p *Processor) RetranslateSubtitles(ctx context.Context, input string, options RetranslateOptions) (RetranslateResult, error) {
	if p == nil || !p.Enabled() {
		return RetranslateResult{}, fmt.Errorf("localization is disabled")
	}
	input = strings.TrimSpace(input)
	if input == "" {
		return RetranslateResult{}, fmt.Errorf("input video path is required")
	}
	if _, err := os.Stat(input); err != nil {
		return RetranslateResult{}, fmt.Errorf("input video does not exist: %w", err)
	}
	if _, err := exec.LookPath(p.python); err != nil {
		return RetranslateResult{}, fmt.Errorf("python runtime not found: %w", err)
	}

	script := strings.TrimSpace(os.Getenv("SUBTITLE_RETRANSLATE_SCRIPT"))
	if script == "" {
		script = firstExisting(
			"/app/scripts/retranslate_subtitles.py",
			filepath.FromSlash("scripts/retranslate_subtitles.py"),
		)
	}
	if script == "" {
		return RetranslateResult{}, fmt.Errorf("subtitle retranslation script was not found")
	}

	profile := strings.ToLower(strings.TrimSpace(options.Profile))
	if profile == "" {
		profile = "auto"
	}
	switch profile {
	case "auto", "drama", "short_drama", "affiliate", "tutorial", "general":
	default:
		return RetranslateResult{}, fmt.Errorf("unsupported translation profile %q", profile)
	}
	instruction := strings.TrimSpace(options.Instruction)
	if len([]rune(instruction)) > 4000 {
		return RetranslateResult{}, fmt.Errorf("translation instruction is too long")
	}

	select {
	case p.sem <- struct{}{}:
		defer func() { <-p.sem }()
	case <-ctx.Done():
		return RetranslateResult{}, ctx.Err()
	}

	outputDir := filepath.Join(filepath.Dir(input), "localized")
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return RetranslateResult{}, fmt.Errorf("create localization output directory: %w", err)
	}

	args := []string{
		script,
		"--input", input,
		"--output-dir", outputDir,
		"--profile", profile,
	}
	if instruction != "" {
		args = append(args, "--instruction", instruction)
	}
	if len(options.SegmentIDs) > 0 {
		parts := make([]string, 0, len(options.SegmentIDs))
		seen := make(map[int]struct{}, len(options.SegmentIDs))
		for _, id := range options.SegmentIDs {
			if id < 0 {
				return RetranslateResult{}, fmt.Errorf("segment ids must be non-negative")
			}
			if _, ok := seen[id]; ok {
				continue
			}
			seen[id] = struct{}{}
			parts = append(parts, strconv.Itoa(id))
		}
		if len(parts) > 0 {
			args = append(args, "--segment-ids", strings.Join(parts, ","))
		}
	}

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
		return RetranslateResult{}, fmt.Errorf("subtitle retranslation failed: %s", message)
	}

	var result RetranslateResult
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &result); err != nil {
		return RetranslateResult{}, fmt.Errorf("decode subtitle retranslation result: %w; output=%q", err, strings.TrimSpace(stdout.String()))
	}
	if result.Segments <= 0 || strings.TrimSpace(result.VietnameseSubtitle) == "" {
		return RetranslateResult{}, fmt.Errorf("subtitle retranslation finished without subtitle output")
	}
	return result, nil
}

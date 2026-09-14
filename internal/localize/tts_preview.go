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

type TTSPreviewOptions struct {
	Text        string
	DurationSec float64
	SpeechRate  string
	Voice       string
	VoiceGender string
}

type TTSPreviewResult struct {
	Audio      []byte `json:"-"`
	Voice      string `json:"voice,omitempty"`
	SpeechRate string `json:"speechRate,omitempty"`
	DurationMs int    `json:"durationMs,omitempty"`
	SlotMs     int    `json:"slotMs,omitempty"`
}

// PreviewTTS synthesizes exactly one utterance and returns MP3 bytes. It does
// not touch subtitle caches, the rendered MP4, or the persistent TTS track.
func (p *Processor) PreviewTTS(ctx context.Context, options TTSPreviewOptions) (TTSPreviewResult, error) {
	if p == nil || !p.Enabled() {
		return TTSPreviewResult{}, fmt.Errorf("localization is disabled")
	}
	text := strings.TrimSpace(options.Text)
	if text == "" {
		return TTSPreviewResult{}, fmt.Errorf("preview text is required")
	}
	if len([]rune(text)) > 1200 {
		return TTSPreviewResult{}, fmt.Errorf("preview text is too long")
	}
	if _, err := exec.LookPath(p.python); err != nil {
		return TTSPreviewResult{}, fmt.Errorf("python runtime not found: %w", err)
	}

	script := strings.TrimSpace(os.Getenv("TTS_PREVIEW_SCRIPT"))
	if script == "" {
		script = firstExisting(
			"/app/scripts/tts_preview.py",
			filepath.FromSlash("scripts/tts_preview.py"),
		)
	}
	if script == "" {
		return TTSPreviewResult{}, fmt.Errorf("TTS preview script was not found")
	}

	duration := options.DurationSec
	if duration <= 0 {
		duration = 3
	}
	if duration > 30 {
		duration = 30
	}

	tempDir, err := os.MkdirTemp("", "videoget-tts-preview-")
	if err != nil {
		return TTSPreviewResult{}, err
	}
	defer os.RemoveAll(tempDir)
	output := filepath.Join(tempDir, "preview.mp3")

	cmd := exec.CommandContext(
		ctx,
		p.python,
		script,
		"--text", text,
		"--duration", strconv.FormatFloat(duration, 'f', 3, 64),
		"--speech-rate", strings.TrimSpace(options.SpeechRate),
		"--voice", strings.TrimSpace(options.Voice),
		"--voice-gender", strings.TrimSpace(options.VoiceGender),
		"--output", output,
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
		return TTSPreviewResult{}, fmt.Errorf("TTS preview failed: %s", message)
	}

	var metadata struct {
		Voice      string `json:"voice"`
		SpeechRate string `json:"speechRate"`
		DurationMs int    `json:"durationMs"`
		SlotMs     int    `json:"slotMs"`
	}
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &metadata); err != nil {
		return TTSPreviewResult{}, fmt.Errorf("decode TTS preview result: %w", err)
	}
	audio, err := os.ReadFile(output)
	if err != nil {
		return TTSPreviewResult{}, fmt.Errorf("read TTS preview audio: %w", err)
	}
	if len(audio) < 256 {
		return TTSPreviewResult{}, fmt.Errorf("TTS preview returned empty audio")
	}
	return TTSPreviewResult{
		Audio:      audio,
		Voice:      metadata.Voice,
		SpeechRate: metadata.SpeechRate,
		DurationMs: metadata.DurationMs,
		SlotMs:     metadata.SlotMs,
	}, nil
}

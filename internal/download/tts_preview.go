package download

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/localize"
)

type SubtitleTTSPreviewRequest struct {
	SegmentID  int    `json:"segmentId"`
	Text       string `json:"text,omitempty"`
	SpeechRate string `json:"speechRate,omitempty"`
	Voice      string `json:"voice,omitempty"`
	VoiceGender string `json:"voiceGender,omitempty"`
}

type SubtitleTTSPreview struct {
	Audio      []byte
	Voice      string
	SpeechRate string
	DurationMs int
	SlotMs     int
}

func (m *Manager) PreviewSubtitleTTS(id string, request SubtitleTTSPreviewRequest) (SubtitleTTSPreview, error) {
	id = strings.TrimSpace(id)
	if _, ok := m.Get(id); !ok {
		return SubtitleTTSPreview{}, fmt.Errorf("job not found")
	}
	if m.localizer == nil || !m.localizer.Enabled() {
		return SubtitleTTSPreview{}, fmt.Errorf("localization is disabled")
	}
	doc, err := m.GetSubtitles(id)
	if err != nil {
		return SubtitleTTSPreview{}, err
	}
	var selected *SubtitleSegment
	for index := range doc.Segments {
		if doc.Segments[index].ID == request.SegmentID {
			selected = &doc.Segments[index]
			break
		}
	}
	if selected == nil {
		return SubtitleTTSPreview{}, fmt.Errorf("subtitle segment not found")
	}

	text := strings.TrimSpace(request.Text)
	if text == "" {
		text = strings.TrimSpace(selected.Text)
	}
	if text == "" {
		return SubtitleTTSPreview{}, fmt.Errorf("preview text is empty")
	}
	if len([]rune(text)) > 1200 {
		return SubtitleTTSPreview{}, fmt.Errorf("preview text is too long")
	}
	rate := strings.TrimSpace(request.SpeechRate)
	if rate == "" {
		rate = defaultSpeechRate(selected.SpeechRate)
	}
	voice := strings.TrimSpace(request.Voice)
	if voice == "" {
		voice = strings.TrimSpace(selected.Voice)
	}
	gender := strings.TrimSpace(request.VoiceGender)
	if gender == "" {
		gender = strings.TrimSpace(selected.VoiceGender)
	}
	duration := selected.End - selected.Start
	if duration < 0.5 {
		duration = 0.5
	}

	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	result, err := m.localizer.PreviewTTS(ctx, localize.TTSPreviewOptions{
		Text:        text,
		DurationSec: duration,
		SpeechRate:  rate,
		Voice:       voice,
		VoiceGender: gender,
	})
	if err != nil {
		return SubtitleTTSPreview{}, err
	}
	return SubtitleTTSPreview{
		Audio:      result.Audio,
		Voice:      result.Voice,
		SpeechRate: result.SpeechRate,
		DurationMs: result.DurationMs,
		SlotMs:     result.SlotMs,
	}, nil
}

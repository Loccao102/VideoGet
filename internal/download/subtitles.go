package download

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type SubtitleSegment struct {
	ID                    int     `json:"id"`
	SourceSegmentIDs      []int   `json:"sourceSegmentIds,omitempty"`
	SceneID               int     `json:"sceneId,omitempty"`
	Start                 float64 `json:"start"`
	End                   float64 `json:"end"`
	SourceText            string  `json:"sourceText,omitempty"`
	SourceCorrected       string  `json:"sourceCorrected,omitempty"`
	Text                  string  `json:"text"`
	SpeechRate            string  `json:"speechRate,omitempty"`
	AppliedSpeechRate     string  `json:"appliedSpeechRate,omitempty"`
	UtteranceID           string  `json:"utteranceId,omitempty"`
	Speaker               string  `json:"speaker,omitempty"`
	TranslationConfidence float64 `json:"translationConfidence,omitempty"`
	Voice                 string  `json:"voice,omitempty"`
	VoiceGender           string  `json:"voiceGender,omitempty"`
	AppliedVoice          string  `json:"appliedVoice,omitempty"`
}

type SubtitleDocument struct {
	JobID       string            `json:"jobId"`
	SourceVideo string            `json:"sourceVideo"`
	OutputVideo string            `json:"outputVideo,omitempty"`
	Segments    []SubtitleSegment `json:"segments"`
	Draft       bool              `json:"draft"`
	EditedAt    string            `json:"editedAt,omitempty"`
}

type SubtitleUpdate struct {
	Segments []SubtitleSegment `json:"segments"`
}

type pipelineSubtitleSegment struct {
	ID                    int     `json:"id"`
	SourceSegmentIDs      []int   `json:"sourceSegmentIds,omitempty"`
	SceneID               int     `json:"sceneId,omitempty"`
	Start                 float64 `json:"start"`
	End                   float64 `json:"end"`
	Text                  string  `json:"text,omitempty"`
	SourceCorrected       string  `json:"sourceCorrected,omitempty"`
	VI                    string  `json:"vi,omitempty"`
	SpeechRate            string  `json:"speechRate,omitempty"`
	AppliedSpeechRate     string  `json:"appliedSpeechRate,omitempty"`
	UtteranceID           string  `json:"utteranceId,omitempty"`
	Speaker               string  `json:"speaker,omitempty"`
	TranslationConfidence float64 `json:"translationConfidence,omitempty"`
	Voice                 string  `json:"voice,omitempty"`
	VoiceGender           string  `json:"voiceGender,omitempty"`
	AppliedVoice          string  `json:"appliedVoice,omitempty"`
}

type subtitleDraft struct {
	Version  int                       `json:"version"`
	JobID    string                    `json:"jobId"`
	EditedAt string                    `json:"editedAt"`
	Segments []pipelineSubtitleSegment `json:"segments"`
}

func (m *Manager) subtitlePaths(job Job) (localizedDir, stem, draftPath, translatedPath, metadataPath, viSRT, ttsCache, voiceTrack string, err error) {
	if !reusableMedia(job.SourceOutput) {
		return "", "", "", "", "", "", "", "", fmt.Errorf("job source video is unavailable")
	}
	stem = strings.TrimSuffix(filepath.Base(job.SourceOutput), filepath.Ext(job.SourceOutput))
	localizedDir = filepath.Join(filepath.Dir(job.SourceOutput), "localized")
	draftPath = filepath.Join(localizedDir, stem+".subtitle-edit.json")
	translatedPath = filepath.Join(localizedDir, stem+".translated.json")
	metadataPath = filepath.Join(localizedDir, stem+".localization.json")
	viSRT = filepath.Join(localizedDir, stem+".vi.srt")
	ttsCache = filepath.Join(localizedDir, stem+".tts.json")
	voiceTrack = filepath.Join(localizedDir, stem+".vi-voice.wav")
	return
}

func (m *Manager) GetSubtitles(id string) (SubtitleDocument, error) {
	job, ok := m.Get(strings.TrimSpace(id))
	if !ok {
		return SubtitleDocument{}, fmt.Errorf("job not found")
	}
	_, _, draftPath, translatedPath, metadataPath, _, _, _, err := m.subtitlePaths(job)
	if err != nil {
		return SubtitleDocument{}, err
	}

	segments := []pipelineSubtitleSegment{}
	draft := false
	editedAt := ""
	if data, readErr := os.ReadFile(draftPath); readErr == nil {
		var value subtitleDraft
		if json.Unmarshal(data, &value) == nil && len(value.Segments) > 0 {
			segments = value.Segments
			draft = true
			editedAt = value.EditedAt
		}
	}
	if len(segments) == 0 {
		segments, err = readSubtitleSegments(translatedPath)
		if err != nil || len(segments) == 0 {
			segments, err = readSubtitleSegments(metadataPath)
		}
	}
	if err != nil || len(segments) == 0 {
		return SubtitleDocument{}, fmt.Errorf("localized subtitle segments are unavailable")
	}

	return SubtitleDocument{
		JobID:       job.ID,
		SourceVideo: job.SourceOutput,
		OutputVideo: job.Output,
		Segments:    toSubtitleSegments(segments),
		Draft:       draft,
		EditedAt:    editedAt,
	}, nil
}

func readSubtitleSegments(path string) ([]pipelineSubtitleSegment, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var payload struct {
		Segments []pipelineSubtitleSegment `json:"segments"`
	}
	if err := json.Unmarshal(data, &payload); err != nil {
		return nil, err
	}
	return payload.Segments, nil
}

func toSubtitleSegments(items []pipelineSubtitleSegment) []SubtitleSegment {
	out := make([]SubtitleSegment, 0, len(items))
	for _, item := range items {
		out = append(out, SubtitleSegment{
			ID:                    item.ID,
			SourceSegmentIDs:      append([]int(nil), item.SourceSegmentIDs...),
			SceneID:               item.SceneID,
			Start:                 item.Start,
			End:                   item.End,
			SourceText:            item.Text,
			SourceCorrected:       item.SourceCorrected,
			Text:                  item.VI,
			SpeechRate:            defaultSpeechRate(item.SpeechRate),
			AppliedSpeechRate:     item.AppliedSpeechRate,
			UtteranceID:           item.UtteranceID,
			Speaker:               item.Speaker,
			TranslationConfidence: item.TranslationConfidence,
			Voice:                 item.Voice,
			VoiceGender:           defaultVoiceGender(item.VoiceGender),
			AppliedVoice:          item.AppliedVoice,
		})
	}
	return out
}

func defaultSpeechRate(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "auto"
	}
	return value
}

func defaultVoiceGender(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "male" || value == "female" {
		return value
	}
	return "auto"
}

func (m *Manager) SaveSubtitles(id string, update SubtitleUpdate) (SubtitleDocument, error) {
	job, ok := m.Get(strings.TrimSpace(id))
	if !ok {
		return SubtitleDocument{}, fmt.Errorf("job not found")
	}
	localizedDir, _, draftPath, translatedPath, metadataPath, viSRT, ttsCache, voiceTrack, err := m.subtitlePaths(job)
	if err != nil {
		return SubtitleDocument{}, err
	}
	if len(update.Segments) == 0 {
		return SubtitleDocument{}, fmt.Errorf("at least one subtitle segment is required")
	}
	if len(update.Segments) > 3000 {
		return SubtitleDocument{}, fmt.Errorf("too many subtitle segments")
	}
	if err := os.MkdirAll(localizedDir, 0o755); err != nil {
		return SubtitleDocument{}, err
	}

	current, _ := m.GetSubtitles(id)
	currentByID := make(map[int]SubtitleSegment, len(current.Segments))
	for _, item := range current.Segments {
		currentByID[item.ID] = item
	}

	seen := map[int]struct{}{}
	pipeline := make([]pipelineSubtitleSegment, 0, len(update.Segments))
	lastStart := -1.0
	for index, item := range update.Segments {
		if _, exists := seen[item.ID]; exists {
			return SubtitleDocument{}, fmt.Errorf("duplicate subtitle id %d", item.ID)
		}
		seen[item.ID] = struct{}{}
		if math.IsNaN(item.Start) || math.IsNaN(item.End) || math.IsInf(item.Start, 0) || math.IsInf(item.End, 0) {
			return SubtitleDocument{}, fmt.Errorf("segment %d has invalid timing", item.ID)
		}
		if item.Start < 0 || item.End <= item.Start || item.End-item.Start < 0.12 {
			return SubtitleDocument{}, fmt.Errorf("segment %d must have a valid timing slot", item.ID)
		}
		if index > 0 && item.Start < lastStart {
			return SubtitleDocument{}, fmt.Errorf("subtitle segments must be ordered by start time")
		}
		lastStart = item.Start
		text := strings.TrimSpace(item.Text)
		if text == "" {
			return SubtitleDocument{}, fmt.Errorf("segment %d has empty Vietnamese text", item.ID)
		}
		if len([]rune(text)) > 1200 {
			return SubtitleDocument{}, fmt.Errorf("segment %d is too long", item.ID)
		}
		rate, err := normalizeSpeechRate(item.SpeechRate)
		if err != nil {
			return SubtitleDocument{}, fmt.Errorf("segment %d: %w", item.ID, err)
		}

		previous := currentByID[item.ID]
		source := strings.TrimSpace(item.SourceText)
		if source == "" {
			source = previous.SourceText
		}
		sourceCorrected := strings.TrimSpace(item.SourceCorrected)
		if sourceCorrected == "" {
			sourceCorrected = previous.SourceCorrected
		}
		utteranceID := strings.TrimSpace(item.UtteranceID)
		if utteranceID == "" {
			utteranceID = previous.UtteranceID
		}
		speaker := strings.TrimSpace(item.Speaker)
		if speaker == "" {
			speaker = previous.Speaker
		}
		confidence := item.TranslationConfidence
		if confidence <= 0 {
			confidence = previous.TranslationConfidence
		}
		sourceSegmentIDs := append([]int(nil), item.SourceSegmentIDs...)
		if len(sourceSegmentIDs) == 0 {
			sourceSegmentIDs = append([]int(nil), previous.SourceSegmentIDs...)
		}
		if len(sourceSegmentIDs) == 0 {
			sourceSegmentIDs = []int{item.ID}
		}
		sceneID := item.SceneID
		if sceneID <= 0 {
			sceneID = previous.SceneID
		}
		voice := strings.TrimSpace(item.Voice)
		if voice == "" {
			voice = strings.TrimSpace(previous.Voice)
		}
		voiceGender := strings.TrimSpace(item.VoiceGender)
		if voiceGender == "" {
			voiceGender = previous.VoiceGender
		}
		voiceGender = defaultVoiceGender(voiceGender)

		pipeline = append(pipeline, pipelineSubtitleSegment{
			ID:                    item.ID,
			SourceSegmentIDs:      sourceSegmentIDs,
			SceneID:               sceneID,
			Start:                 item.Start,
			End:                   item.End,
			Text:                  source,
			SourceCorrected:       sourceCorrected,
			VI:                    text,
			SpeechRate:            rate,
			UtteranceID:           utteranceID,
			Speaker:               speaker,
			TranslationConfidence: confidence,
			Voice:                 voice,
			VoiceGender:           voiceGender,
		})
	}

	now := time.Now().UTC().Format(time.RFC3339)
	draft := subtitleDraft{Version: 5, JobID: job.ID, EditedAt: now, Segments: pipeline}
	if err := writeJSONAtomic(draftPath, draft); err != nil {
		return SubtitleDocument{}, err
	}
	if err := writeSubtitleSRT(viSRT, pipeline); err != nil {
		return SubtitleDocument{}, err
	}

	// Keep the persistent worker's translation cache aligned with manual edits.
	// This also preserves contextual utterance/scene/speaker/voice metadata after a restart.
	if data, readErr := os.ReadFile(translatedPath); readErr == nil {
		var payload map[string]any
		if json.Unmarshal(data, &payload) == nil {
			payload["segments"] = pipeline
			_ = writeJSONAtomic(translatedPath, payload)
		}
	}
	if data, readErr := os.ReadFile(metadataPath); readErr == nil {
		var payload map[string]any
		if json.Unmarshal(data, &payload) == nil {
			payload["segments"] = pipeline
			payload["subtitleDraftPending"] = true
			payload["subtitleEditedAt"] = now
			_ = writeJSONAtomic(metadataPath, payload)
		}
	}

	// A text/timing/voice edit invalidates generated voice. Keep the old final MP4
	// so the user can still compare it until the explicit re-render finishes.
	_ = os.Remove(ttsCache)
	_ = os.Remove(voiceTrack)

	return SubtitleDocument{
		JobID:       job.ID,
		SourceVideo: job.SourceOutput,
		OutputVideo: job.Output,
		Segments:    toSubtitleSegments(pipeline),
		Draft:       true,
		EditedAt:    now,
	}, nil
}

func normalizeSpeechRate(value string) (string, error) {
	value = strings.TrimSpace(strings.ToLower(value))
	if value == "" || value == "auto" {
		return "auto", nil
	}
	numeric := strings.TrimSuffix(value, "%")
	n, err := strconv.Atoi(numeric)
	if err != nil || n < -50 || n > 100 {
		return "", fmt.Errorf("speechRate must be auto or between -50%% and +100%%")
	}
	return fmt.Sprintf("%+d%%", n), nil
}

func writeJSONAtomic(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	temp := path + ".tmp"
	if err := os.WriteFile(temp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(temp, path)
}

func writeSubtitleSRT(path string, segments []pipelineSubtitleSegment) error {
	var builder strings.Builder
	index := 1
	for _, segment := range segments {
		text := strings.TrimSpace(strings.ReplaceAll(segment.VI, "\n", " "))
		if text == "" {
			continue
		}
		builder.WriteString(strconv.Itoa(index))
		builder.WriteString("\n")
		builder.WriteString(subtitleSRTTime(segment.Start))
		builder.WriteString(" --> ")
		builder.WriteString(subtitleSRTTime(segment.End))
		builder.WriteString("\n")
		builder.WriteString(text)
		builder.WriteString("\n\n")
		index++
	}
	return os.WriteFile(path, []byte(builder.String()), 0o644)
}

func subtitleSRTTime(seconds float64) string {
	milliseconds := int64(math.Round(math.Max(0, seconds) * 1000))
	hours := milliseconds / 3_600_000
	milliseconds %= 3_600_000
	minutes := milliseconds / 60_000
	milliseconds %= 60_000
	secs := milliseconds / 1000
	milliseconds %= 1000
	return fmt.Sprintf("%02d:%02d:%02d,%03d", hours, minutes, secs, milliseconds)
}

func (m *Manager) RerenderSubtitles(id string) (Job, error) {
	id = strings.TrimSpace(id)
	m.mu.Lock()
	job, ok := m.jobs[id]
	if !ok {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job not found")
	}
	if !reusableMedia(job.SourceOutput) {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job source video is unavailable")
	}
	switch job.Status {
	case JobQueued, JobDownloading, JobLocalizing:
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job is already running")
	}
	_, _, draftPath, _, _, _, _, _, pathErr := m.subtitlePaths(job)
	if pathErr != nil {
		m.mu.Unlock()
		return Job{}, pathErr
	}
	if _, err := os.Stat(draftPath); err != nil {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("save subtitle edits before re-rendering")
	}
	job.Status = JobLocalizing
	job.Error = ""
	job.Attempts++
	job.UpdatedAt = time.Now().UTC()
	m.jobs[id] = job
	m.mu.Unlock()
	if err := m.store.Upsert(job); err != nil {
		return Job{}, err
	}
	go m.runSubtitleRerender(id)
	return job, nil
}

func (m *Manager) runSubtitleRerender(id string) {
	job, ok := m.Get(id)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(envPositiveInt("JOB_TIMEOUT_MINUTES", 180))*time.Minute)
	activeState, active := m.registerActiveJob(id, cancel)
	if !active {
		cancel()
		return
	}
	defer func() {
		cancel()
		m.unregisterActiveJob(id, activeState)
	}()

	if m.localizer == nil || !m.localizer.Enabled() {
		m.fail(id, JobLocalizationFailed, fmt.Errorf("localization is disabled"))
		return
	}
	result, err := m.localizer.RerenderSubtitles(ctx, job.SourceOutput)
	if err != nil {
		m.fail(id, JobLocalizationFailed, err)
		return
	}
	m.update(id, func(job *Job) {
		job.Status = JobDone
		job.Localization = &result
		job.Error = ""
		job.Output = result.OutputVideo
		job.UpdatedAt = time.Now().UTC()
	})
}

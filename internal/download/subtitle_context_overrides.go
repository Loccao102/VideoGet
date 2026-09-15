package download

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type SubtitleCharacterOverride struct {
	Source        string `json:"source"`
	PreferredName string `json:"preferredName"`
	Role          string `json:"role,omitempty"`
	Gender        string `json:"gender,omitempty"`
	VoiceGender   string `json:"voiceGender,omitempty"`
}

type SubtitleRelationshipOverride struct {
	From     string `json:"from"`
	To       string `json:"to"`
	Relation string `json:"relation"`
}

type SubtitleTermOverride struct {
	Source        string `json:"source"`
	PreferredText string `json:"preferredText"`
	Note          string `json:"note,omitempty"`
}

type SubtitleContextOverrides struct {
	Version       int                            `json:"version"`
	JobID         string                         `json:"jobId"`
	UpdatedAt     string                         `json:"updatedAt,omitempty"`
	Characters    []SubtitleCharacterOverride    `json:"characters"`
	Relationships []SubtitleRelationshipOverride `json:"relationships"`
	Terms         []SubtitleTermOverride         `json:"terms"`
	Notes         string                         `json:"notes,omitempty"`
}

func (m *Manager) subtitleContextOverridesPath(job Job) (string, error) {
	localizedDir, stem, _, _, _, _, _, _, err := m.subtitlePaths(job)
	if err != nil {
		return "", err
	}
	return filepath.Join(localizedDir, stem+".translation-overrides.json"), nil
}

func (m *Manager) GetSubtitleContextOverrides(id string) (SubtitleContextOverrides, error) {
	job, ok := m.Get(strings.TrimSpace(id))
	if !ok {
		return SubtitleContextOverrides{}, fmt.Errorf("job not found")
	}
	path, err := m.subtitleContextOverridesPath(job)
	if err != nil {
		return SubtitleContextOverrides{}, err
	}
	result := SubtitleContextOverrides{
		Version:       1,
		JobID:         job.ID,
		Characters:    []SubtitleCharacterOverride{},
		Relationships: []SubtitleRelationshipOverride{},
		Terms:         []SubtitleTermOverride{},
	}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return result, nil
	}
	if err != nil {
		return SubtitleContextOverrides{}, fmt.Errorf("read subtitle context overrides: %w", err)
	}
	if err := json.Unmarshal(data, &result); err != nil {
		return SubtitleContextOverrides{}, fmt.Errorf("invalid subtitle context overrides: %w", err)
	}
	result.Version = 1
	result.JobID = job.ID
	if result.Characters == nil {
		result.Characters = []SubtitleCharacterOverride{}
	}
	if result.Relationships == nil {
		result.Relationships = []SubtitleRelationshipOverride{}
	}
	if result.Terms == nil {
		result.Terms = []SubtitleTermOverride{}
	}
	return result, nil
}

func (m *Manager) SaveSubtitleContextOverrides(id string, value SubtitleContextOverrides) (SubtitleContextOverrides, error) {
	job, ok := m.Get(strings.TrimSpace(id))
	if !ok {
		return SubtitleContextOverrides{}, fmt.Errorf("job not found")
	}
	path, err := m.subtitleContextOverridesPath(job)
	if err != nil {
		return SubtitleContextOverrides{}, err
	}
	if len(value.Characters) > 100 || len(value.Relationships) > 150 || len(value.Terms) > 150 {
		return SubtitleContextOverrides{}, fmt.Errorf("too many approved context facts")
	}
	if len([]rune(strings.TrimSpace(value.Notes))) > 4000 {
		return SubtitleContextOverrides{}, fmt.Errorf("context notes are too long")
	}

	characters := make([]SubtitleCharacterOverride, 0, len(value.Characters))
	seenCharacters := map[string]struct{}{}
	for _, item := range value.Characters {
		item.Source = strings.TrimSpace(item.Source)
		item.PreferredName = strings.TrimSpace(item.PreferredName)
		item.Role = strings.TrimSpace(item.Role)
		item.Gender = normalizeContextGender(item.Gender)
		item.VoiceGender = normalizeVoiceGender(item.VoiceGender)
		if item.Source == "" || item.PreferredName == "" {
			continue
		}
		if len([]rune(item.Source)) > 160 || len([]rune(item.PreferredName)) > 160 || len([]rune(item.Role)) > 240 {
			return SubtitleContextOverrides{}, fmt.Errorf("character context value is too long")
		}
		key := strings.ToLower(item.Source)
		if _, exists := seenCharacters[key]; exists {
			continue
		}
		seenCharacters[key] = struct{}{}
		characters = append(characters, item)
	}

	relationships := make([]SubtitleRelationshipOverride, 0, len(value.Relationships))
	seenRelationships := map[string]struct{}{}
	for _, item := range value.Relationships {
		item.From = strings.TrimSpace(item.From)
		item.To = strings.TrimSpace(item.To)
		item.Relation = strings.TrimSpace(item.Relation)
		if item.From == "" || item.To == "" || item.Relation == "" {
			continue
		}
		if len([]rune(item.From)) > 160 || len([]rune(item.To)) > 160 || len([]rune(item.Relation)) > 240 {
			return SubtitleContextOverrides{}, fmt.Errorf("relationship context value is too long")
		}
		key := strings.ToLower(item.From + "\x00" + item.To + "\x00" + item.Relation)
		if _, exists := seenRelationships[key]; exists {
			continue
		}
		seenRelationships[key] = struct{}{}
		relationships = append(relationships, item)
	}

	terms := make([]SubtitleTermOverride, 0, len(value.Terms))
	seenTerms := map[string]struct{}{}
	for _, item := range value.Terms {
		item.Source = strings.TrimSpace(item.Source)
		item.PreferredText = strings.TrimSpace(item.PreferredText)
		item.Note = strings.TrimSpace(item.Note)
		if item.Source == "" || item.PreferredText == "" {
			continue
		}
		if len([]rune(item.Source)) > 240 || len([]rune(item.PreferredText)) > 240 || len([]rune(item.Note)) > 500 {
			return SubtitleContextOverrides{}, fmt.Errorf("term context value is too long")
		}
		key := strings.ToLower(item.Source)
		if _, exists := seenTerms[key]; exists {
			continue
		}
		seenTerms[key] = struct{}{}
		terms = append(terms, item)
	}

	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return SubtitleContextOverrides{}, err
	}
	result := SubtitleContextOverrides{
		Version:       1,
		JobID:         job.ID,
		UpdatedAt:     time.Now().UTC().Format(time.RFC3339),
		Characters:    characters,
		Relationships: relationships,
		Terms:         terms,
		Notes:         strings.TrimSpace(value.Notes),
	}
	if err := writeJSONAtomic(path, result); err != nil {
		return SubtitleContextOverrides{}, err
	}
	return result, nil
}

func normalizeContextGender(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "male", "female":
		return strings.ToLower(strings.TrimSpace(value))
	default:
		return "unknown"
	}
}

func normalizeVoiceGender(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "male", "female":
		return strings.ToLower(strings.TrimSpace(value))
	default:
		return "auto"
	}
}

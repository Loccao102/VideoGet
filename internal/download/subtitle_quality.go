package download

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type SubtitleQualityIssue struct {
	SegmentID any    `json:"segmentId"`
	Severity  string `json:"severity"`
	Code      string `json:"code"`
	Detail    string `json:"detail"`
}

type SubtitleQualityReport struct {
	Version  int                    `json:"version"`
	Status   string                 `json:"status"`
	Segments int                    `json:"segments"`
	Errors   int                    `json:"errors"`
	Warnings int                    `json:"warnings"`
	Issues   []SubtitleQualityIssue `json:"issues"`
	Settings map[string]any         `json:"settings,omitempty"`
}

type SubtitleQualityDocument struct {
	JobID              string                `json:"jobId"`
	Report             SubtitleQualityReport `json:"report"`
	SemanticBlockCount int                   `json:"semanticBlockCount"`
	QAFile             string                `json:"qaFile"`
	SemanticBlocksFile string                `json:"semanticBlocksFile"`
}

func (m *Manager) GetSubtitleQuality(id string) (SubtitleQualityDocument, error) {
	job, ok := m.Get(strings.TrimSpace(id))
	if !ok {
		return SubtitleQualityDocument{}, fmt.Errorf("job not found")
	}
	localizedDir, stem, _, _, _, _, _, _, err := m.subtitlePaths(job)
	if err != nil {
		return SubtitleQualityDocument{}, err
	}

	qaPath := filepath.Join(localizedDir, stem+".translation-qa.json")
	blocksPath := filepath.Join(localizedDir, stem+".semantic-blocks.json")
	qaData, err := os.ReadFile(qaPath)
	if err != nil {
		return SubtitleQualityDocument{}, fmt.Errorf("translation QA report is unavailable")
	}
	var report SubtitleQualityReport
	if err := json.Unmarshal(qaData, &report); err != nil {
		return SubtitleQualityDocument{}, fmt.Errorf("invalid translation QA report: %w", err)
	}

	blockCount := 0
	if blockData, readErr := os.ReadFile(blocksPath); readErr == nil {
		var payload struct {
			Blocks []json.RawMessage `json:"blocks"`
		}
		if json.Unmarshal(blockData, &payload) == nil {
			blockCount = len(payload.Blocks)
		}
	}

	return SubtitleQualityDocument{
		JobID:              job.ID,
		Report:             report,
		SemanticBlockCount: blockCount,
		QAFile:             qaPath,
		SemanticBlocksFile: blocksPath,
	}, nil
}

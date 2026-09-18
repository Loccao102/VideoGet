package download

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
)

const (
	OutputAspectOriginal = "original"
	OutputAspect16x9      = "16:9"
	OutputAspect3x4       = "3:4"
	OutputAspect9x16      = "9:16"
	OutputAspect1x1       = "1:1"
)

func validateOutputAspect(value string) (string, error) {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		value = OutputAspectOriginal
	}
	switch value {
	case OutputAspectOriginal, OutputAspect16x9, OutputAspect3x4, OutputAspect9x16, OutputAspect1x1:
		return value, nil
	default:
		return "", fmt.Errorf("output aspect must be original, 16:9, 3:4, 9:16, or 1:1")
	}
}

func normalizeOutputAspect(value string) string {
	aspect, err := validateOutputAspect(value)
	if err != nil {
		return OutputAspectOriginal
	}
	return aspect
}

func aspectSuffix(aspect string) string {
	return strings.NewReplacer(":", "x", "/", "x", " ", "").Replace(normalizeOutputAspect(aspect))
}

func aspectOutputPath(input, aspect string) string {
	ext := filepath.Ext(input)
	stem := strings.TrimSuffix(filepath.Base(input), ext)
	return filepath.Join(filepath.Dir(input), fmt.Sprintf("%s.aspect-%s.mp4", stem, aspectSuffix(aspect)))
}

func (m *Manager) applyOutputAspect(ctx context.Context, input, aspect string) (string, error) {
	aspect = normalizeOutputAspect(aspect)
	if aspect == OutputAspectOriginal {
		return input, nil
	}
	if m.localizer == nil {
		return "", fmt.Errorf("aspect converter is unavailable")
	}
	output := aspectOutputPath(input, aspect)
	if err := m.localizer.ConvertAspect(ctx, input, output, aspect); err != nil {
		return "", err
	}
	return output, nil
}

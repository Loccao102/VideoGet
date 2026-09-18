package httpapi

import (
	"strings"
	"unicode"

	"github.com/Loccao102/VideoGet/internal/model"
)

const (
	searchModeKeyword = "keyword"
	searchModeChannel = "channel"
)

func normalizeSearchMode(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case searchModeChannel, "creator", "author", "uploader":
		return searchModeChannel
	default:
		return searchModeKeyword
	}
}

func normalizeChannelName(value string) string {
	value = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(value), "@"))
	if value == "" {
		return ""
	}
	var b strings.Builder
	lastSpace := false
	for _, r := range strings.ToLower(value) {
		if unicode.IsSpace(r) {
			if !lastSpace {
				b.WriteByte(' ')
				lastSpace = true
			}
			continue
		}
		// Ignore common visual separators so "Mimi Official" and
		// "@Mimi_Official" can still match after normalization.
		switch r {
		case '_', '-', '·', '•':
			if !lastSpace {
				b.WriteByte(' ')
				lastSpace = true
			}
			continue
		}
		b.WriteRune(r)
		lastSpace = false
	}
	return strings.Join(strings.Fields(b.String()), " ")
}

func filterVideosByChannel(videos []model.Video, channel string) []model.Video {
	want := normalizeChannelName(channel)
	if want == "" {
		return nil
	}
	filtered := make([]model.Video, 0, len(videos))
	for _, video := range videos {
		if normalizeChannelName(video.Author) == want {
			filtered = append(filtered, video)
		}
	}
	return filtered
}

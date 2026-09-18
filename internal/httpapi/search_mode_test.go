package httpapi

import (
	"testing"

	"github.com/Loccao102/VideoGet/internal/model"
)

func TestNormalizeSearchMode(t *testing.T) {
	if got := normalizeSearchMode("channel"); got != searchModeChannel {
		t.Fatalf("channel mode = %q", got)
	}
	if got := normalizeSearchMode("creator"); got != searchModeChannel {
		t.Fatalf("creator alias = %q", got)
	}
	if got := normalizeSearchMode(""); got != searchModeKeyword {
		t.Fatalf("default mode = %q", got)
	}
}

func TestNormalizeChannelName(t *testing.T) {
	cases := map[string]string{
		" @Mimi_Official ": "mimi official",
		"Mimi-Official":    "mimi official",
		"干嘛猫":              "干嘛猫",
		"  干嘛猫  ":          "干嘛猫",
	}
	for input, want := range cases {
		if got := normalizeChannelName(input); got != want {
			t.Fatalf("normalizeChannelName(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestFilterVideosByChannelOnlyKeepsExactAuthor(t *testing.T) {
	videos := []model.Video{
		{ID: "1", Author: "干嘛猫"},
		{ID: "2", Author: "干嘛猫 Official"},
		{ID: "3", Author: ""},
		{ID: "4", Author: "别的频道"},
	}
	got := filterVideosByChannel(videos, "干嘛猫")
	if len(got) != 1 || got[0].ID != "1" {
		t.Fatalf("filtered = %#v, want only video 1", got)
	}
}

func TestFilterVideosByChannelNormalizesAtAndSeparators(t *testing.T) {
	videos := []model.Video{
		{ID: "1", Author: "Mimi Official"},
		{ID: "2", Author: "Mimi Studio"},
	}
	got := filterVideosByChannel(videos, "@Mimi_Official")
	if len(got) != 1 || got[0].ID != "1" {
		t.Fatalf("filtered = %#v, want only video 1", got)
	}
}

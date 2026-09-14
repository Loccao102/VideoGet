package download

import "testing"

func TestNormalizeSpeechRate(t *testing.T) {
	cases := map[string]string{
		"":      "auto",
		"auto":  "auto",
		"+20%":  "+20%",
		"20":    "+20%",
		"-10%":  "-10%",
		"+100%": "+100%",
	}
	for input, want := range cases {
		got, err := normalizeSpeechRate(input)
		if err != nil {
			t.Fatalf("normalizeSpeechRate(%q): %v", input, err)
		}
		if got != want {
			t.Fatalf("normalizeSpeechRate(%q)=%q, want %q", input, got, want)
		}
	}
	for _, input := range []string{"abc", "+101%", "-51%"} {
		if _, err := normalizeSpeechRate(input); err == nil {
			t.Fatalf("normalizeSpeechRate(%q) should fail", input)
		}
	}
}

func TestSubtitleSRTTime(t *testing.T) {
	cases := map[float64]string{
		0:       "00:00:00,000",
		1.234:   "00:00:01,234",
		61.005:  "00:01:01,005",
		3661.75: "01:01:01,750",
	}
	for input, want := range cases {
		if got := subtitleSRTTime(input); got != want {
			t.Fatalf("subtitleSRTTime(%v)=%q, want %q", input, got, want)
		}
	}
}

func TestToSubtitleSegmentsPreservesSceneAndVoiceMetadata(t *testing.T) {
	items := []pipelineSubtitleSegment{{
		ID:               12,
		SourceSegmentIDs: []int{12, 13, 14},
		SceneID:          3,
		Start:            4.2,
		End:              7.8,
		Text:             "原始字幕",
		VI:               "Bản dịch theo cả câu.",
		UtteranceID:      "s3:u2",
		VoiceGender:      "female",
		AppliedVoice:     "vi-VN-HoaiMyNeural",
	}}
	got := toSubtitleSegments(items)
	if len(got) != 1 {
		t.Fatalf("got %d segments, want 1", len(got))
	}
	if got[0].SceneID != 3 {
		t.Fatalf("SceneID=%d, want 3", got[0].SceneID)
	}
	if len(got[0].SourceSegmentIDs) != 3 || got[0].SourceSegmentIDs[1] != 13 {
		t.Fatalf("SourceSegmentIDs=%v, want [12 13 14]", got[0].SourceSegmentIDs)
	}
	if got[0].VoiceGender != "female" {
		t.Fatalf("VoiceGender=%q, want female", got[0].VoiceGender)
	}
	if got[0].AppliedVoice != "vi-VN-HoaiMyNeural" {
		t.Fatalf("AppliedVoice=%q", got[0].AppliedVoice)
	}
}

func TestContextGenderNormalization(t *testing.T) {
	if got := normalizeContextGender(" FEMALE "); got != "female" {
		t.Fatalf("normalizeContextGender=%q", got)
	}
	if got := normalizeContextGender("other"); got != "unknown" {
		t.Fatalf("normalizeContextGender(other)=%q", got)
	}
	if got := normalizeVoiceGender("MALE"); got != "male" {
		t.Fatalf("normalizeVoiceGender=%q", got)
	}
	if got := normalizeVoiceGender(""); got != "auto" {
		t.Fatalf("normalizeVoiceGender(empty)=%q", got)
	}
}

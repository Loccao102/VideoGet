package download

import "testing"

func TestValidateProcessingMode(t *testing.T) {
	for _, mode := range []string{ProcessingDownload, ProcessingSubtitles, ProcessingDub} {
		got, err := validateProcessingMode(mode)
		if err != nil || got != mode {
			t.Fatalf("mode %q => %q, %v", mode, got, err)
		}
	}
	if _, err := validateProcessingMode("voice-only"); err == nil {
		t.Fatal("expected invalid processing mode error")
	}
	if got := normalizeProcessingMode(""); got != ProcessingDub {
		t.Fatalf("legacy blank mode must remain dub, got %q", got)
	}
}

func TestValidateSRT(t *testing.T) {
	valid := "1\n00:00:00,000 --> 00:00:02,500\nXin chào\n"
	if err := validateSRT(valid); err != nil {
		t.Fatalf("valid SRT rejected: %v", err)
	}
	if err := validateSRT("không có timing"); err == nil {
		t.Fatal("expected malformed SRT to be rejected")
	}
}

func TestNormalizeSRT(t *testing.T) {
	got := normalizeSRT("\ufeff1\r\n00:00:00,000 --> 00:00:01,000\r\nA\r\n")
	want := "1\n00:00:00,000 --> 00:00:01,000\nA\n"
	if got != want {
		t.Fatalf("normalizeSRT mismatch:\nwant=%q\ngot =%q", want, got)
	}
}

func TestNormalizeOCRRenderStyle(t *testing.T) {
	tests := map[string]string{
		SubtitleRenderOCROverlay: "clean",
		SubtitleRenderOCRClean:   "clean",
		SubtitleRenderOCRCapsule: "capsule",
		SubtitleRenderOCRBox:     "box",
	}
	for input, want := range tests {
		got, ok := normalizeOCRRenderStyle(input)
		if !ok || got != want {
			t.Fatalf("style %q => %q, %v; want %q", input, got, ok, want)
		}
	}
	if _, ok := normalizeOCRRenderStyle("giant_panel"); ok {
		t.Fatal("unknown OCR render style should be rejected")
	}
}

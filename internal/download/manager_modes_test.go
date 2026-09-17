package download

import "testing"

func TestValidateProcessingModeOCRMusic(t *testing.T) {
	mode, err := validateProcessingMode(" OCR_MUSIC ")
	if err != nil {
		t.Fatalf("validateProcessingMode returned error: %v", err)
	}
	if mode != ProcessingOCRMusic {
		t.Fatalf("mode = %q, want %q", mode, ProcessingOCRMusic)
	}
}

func TestValidateProcessingModeOCRSubtitles(t *testing.T) {
	mode, err := validateProcessingMode(" OCR_SUBTITLES ")
	if err != nil {
		t.Fatalf("validateProcessingMode returned error: %v", err)
	}
	if mode != ProcessingOCRSubtitles {
		t.Fatalf("mode = %q, want %q", mode, ProcessingOCRSubtitles)
	}
}

func TestValidateProcessingModeRejectsUnknown(t *testing.T) {
	if _, err := validateProcessingMode("ocr_voice_magic"); err == nil {
		t.Fatal("expected unknown processing mode to fail validation")
	}
}

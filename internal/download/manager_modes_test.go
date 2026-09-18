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


func TestValidateOutputAspect(t *testing.T) {
	for _, value := range []string{"original", "16:9", "3:4", "9:16", "1:1"} {
		got, err := validateOutputAspect(value)
		if err != nil {
			t.Fatalf("validateOutputAspect(%q): %v", value, err)
		}
		if got != value {
			t.Fatalf("validateOutputAspect(%q) = %q", value, got)
		}
	}
}

func TestValidateOutputAspectDefaultsToOriginal(t *testing.T) {
	got, err := validateOutputAspect(" ")
	if err != nil {
		t.Fatalf("validateOutputAspect empty: %v", err)
	}
	if got != OutputAspectOriginal {
		t.Fatalf("aspect = %q, want %q", got, OutputAspectOriginal)
	}
}

func TestValidateOutputAspectRejectsUnknown(t *testing.T) {
	if _, err := validateOutputAspect("4:5"); err == nil {
		t.Fatal("expected unsupported aspect to fail validation")
	}
}

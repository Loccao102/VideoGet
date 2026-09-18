package download

import (
	"path/filepath"
	"testing"
)

func TestAspectOutputPath(t *testing.T) {
	got := aspectOutputPath(filepath.FromSlash("/tmp/final.vi-subbed.mp4"), OutputAspect3x4)
	want := filepath.FromSlash("/tmp/final.vi-subbed.aspect-3x4.mp4")
	if got != want {
		t.Fatalf("aspectOutputPath = %q, want %q", got, want)
	}
}

func TestAspectBaseCandidateFromDerivative(t *testing.T) {
	got := aspectBaseCandidateFromDerivative(filepath.FromSlash("/tmp/final.vi-subbed.aspect-9x16.mp4"))
	want := filepath.FromSlash("/tmp/final.vi-subbed.mp4")
	if got != want {
		t.Fatalf("aspect base = %q, want %q", got, want)
	}
}

func TestAspectBaseCandidateRejectsNormalOutput(t *testing.T) {
	if got := aspectBaseCandidateFromDerivative("/tmp/final.mp4"); got != "" {
		t.Fatalf("unexpected aspect base %q", got)
	}
}

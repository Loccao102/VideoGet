package download

import (
	"os"
	"path/filepath"
	"testing"
)

func TestFindDouyinBrowserBinaryUsesConfiguredAbsolutePath(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("DOUYIN_BROWSER_BIN", executable)

	got, err := findDouyinBrowserBinary()
	if err != nil {
		t.Fatalf("findDouyinBrowserBinary() error = %v", err)
	}
	if got != executable {
		t.Fatalf("findDouyinBrowserBinary() = %q, want %q", got, executable)
	}
}

func TestFindDouyinBrowserBinaryRejectsMissingConfiguredPath(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "missing-chromium")
	t.Setenv("DOUYIN_BROWSER_BIN", missing)

	if _, err := findDouyinBrowserBinary(); err == nil {
		t.Fatal("findDouyinBrowserBinary() expected error for missing configured binary")
	}
}

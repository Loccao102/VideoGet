package download

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"testing"
)

func TestDownloadDouyinMediaResumableContinuesExistingPartial(t *testing.T) {
	payload := bytes.Repeat([]byte("videoget-douyin-range-"), 7000)
	offset := 48 * 1024
	if len(payload) <= offset {
		t.Fatalf("test payload too small")
	}

	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		wantRange := fmt.Sprintf("bytes=%d-", offset)
		if got := r.Header.Get("Range"); got != wantRange {
			t.Errorf("Range = %q, want %q", got, wantRange)
			http.Error(w, "bad range", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "video/mp4")
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", offset, len(payload)-1, len(payload)))
		w.Header().Set("Content-Length", strconv.Itoa(len(payload)-offset))
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write(payload[offset:])
	}))
	defer server.Close()

	dir := t.TempDir()
	output := filepath.Join(dir, "video.mp4")
	if err := os.WriteFile(output+".part", payload[:offset], 0o644); err != nil {
		t.Fatal(err)
	}

	if err := downloadDouyinMediaResumable(context.Background(), server.URL, "https://www.iesdouyin.com/share/video/123/", output, ""); err != nil {
		t.Fatalf("downloadDouyinMediaResumable() error = %v", err)
	}

	got, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("downloaded payload differs: got %d bytes, want %d", len(got), len(payload))
	}
	if requests.Load() != 1 {
		t.Fatalf("requests = %d, want 1", requests.Load())
	}
	if _, err := os.Stat(output + ".part"); !os.IsNotExist(err) {
		t.Fatalf("partial file still exists after finalization")
	}
}

func TestDownloadDouyinMediaResumableRestartsWhenServerIgnoresRange(t *testing.T) {
	payload := bytes.Repeat([]byte("videoget-douyin-full-response-"), 5000)
	offset := 40 * 1024

	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Header.Get("Range") == "" {
			t.Errorf("expected resume Range header")
		}
		w.Header().Set("Content-Type", "video/mp4")
		w.Header().Set("Content-Length", strconv.Itoa(len(payload)))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(payload)
	}))
	defer server.Close()

	dir := t.TempDir()
	output := filepath.Join(dir, "video.mp4")
	if err := os.WriteFile(output+".part", payload[:offset], 0o644); err != nil {
		t.Fatal(err)
	}

	if err := downloadDouyinMediaResumable(context.Background(), server.URL, "", output, ""); err != nil {
		t.Fatalf("downloadDouyinMediaResumable() error = %v", err)
	}

	got, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("server ignored Range but output was not safely restarted")
	}
	if requests.Load() != 1 {
		t.Fatalf("requests = %d, want 1", requests.Load())
	}
}

func TestParseDouyinContentRange(t *testing.T) {
	start, total, ok := parseDouyinContentRange("bytes 65536-131071/262144")
	if !ok || start != 65536 || total != 262144 {
		t.Fatalf("parseDouyinContentRange() = (%d, %d, %v)", start, total, ok)
	}

	if _, _, ok := parseDouyinContentRange("bytes */262144"); ok {
		t.Fatalf("unsatisfied range must not parse as a normal range")
	}
	if got := douyinUnsatisfiedRangeTotal("bytes */262144"); got != 262144 {
		t.Fatalf("douyinUnsatisfiedRangeTotal() = %d, want 262144", got)
	}
}

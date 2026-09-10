package download

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/Loccao102/VideoGet/internal/localize"
	"github.com/Loccao102/VideoGet/internal/model"
)

func TestJobStoreRoundTrip(t *testing.T) {
	store, err := openJobStore(filepath.Join(t.TempDir(), "jobs.db"))
	if err != nil {
		t.Fatalf("openJobStore: %v", err)
	}
	defer store.Close()

	now := time.Now().UTC().Truncate(time.Microsecond)
	job := Job{
		ID:     "job-1",
		Status: JobDone,
		Video: model.Video{
			ID:       "BV123",
			Platform: "bilibili",
			Title:    "test video",
			URL:      "https://www.bilibili.com/video/BV123",
		},
		SourceOutput: "/downloads/job-1/source.mp4",
		Output:       "/downloads/job-1/localized/final.mp4",
		Localization: &localize.Result{
			OutputVideo:      "/downloads/job-1/localized/final.mp4",
			DetectedLanguage: "zh",
			Segments:         12,
		},
		Attempts:  2,
		CreatedAt: now,
		UpdatedAt: now,
	}
	if err := store.Upsert(job); err != nil {
		t.Fatalf("Upsert: %v", err)
	}

	jobs, err := store.LoadAll()
	if err != nil {
		t.Fatalf("LoadAll: %v", err)
	}
	if len(jobs) != 1 {
		t.Fatalf("LoadAll count = %d, want 1", len(jobs))
	}
	got := jobs[0]
	if got.ID != job.ID || got.Status != JobDone || got.Video.ID != "BV123" {
		t.Fatalf("unexpected job round trip: %#v", got)
	}
	if got.Attempts != 2 {
		t.Fatalf("Attempts = %d, want 2", got.Attempts)
	}
	if got.Localization == nil || got.Localization.Segments != 12 || got.Localization.DetectedLanguage != "zh" {
		t.Fatalf("unexpected localization round trip: %#v", got.Localization)
	}
}

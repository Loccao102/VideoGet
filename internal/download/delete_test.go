package download

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

func TestDeleteCancelsAndRemovesJob(t *testing.T) {
	root := t.TempDir()
	store, err := openJobStore(filepath.Join(root, "jobs.db"))
	if err != nil {
		t.Fatalf("openJobStore: %v", err)
	}
	defer store.Close()

	now := time.Now().UTC()
	job := Job{
		ID:        "deadbeef12345678",
		Status:    JobLocalizing,
		Video:     model.Video{ID: "BV123", Platform: "bilibili", URL: "https://example.invalid/video"},
		Attempts:  1,
		CreatedAt: now,
		UpdatedAt: now,
	}
	if err := store.Upsert(job); err != nil {
		t.Fatalf("Upsert: %v", err)
	}

	jobDir := filepath.Join(root, job.ID)
	if err := os.MkdirAll(jobDir, 0o755); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}
	if err := os.WriteFile(filepath.Join(jobDir, "partial.mp4"), []byte("partial"), 0o644); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}

	manager := &Manager{
		jobs:        map[string]Job{job.ID: job},
		downloadDir: root,
		store:       store,
	}

	ctx, cancel := context.WithCancel(context.Background())
	state, ok := manager.registerActiveJob(job.ID, cancel)
	if !ok {
		t.Fatal("registerActiveJob returned false")
	}
	go func() {
		<-ctx.Done()
		manager.unregisterActiveJob(job.ID, state)
	}()

	if err := manager.Delete(job.ID); err != nil {
		t.Fatalf("Delete: %v", err)
	}

	select {
	case <-ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("active job context was not cancelled")
	}

	if _, ok := manager.Get(job.ID); ok {
		t.Fatal("deleted job still exists in manager")
	}
	jobs, err := store.LoadAll()
	if err != nil {
		t.Fatalf("LoadAll: %v", err)
	}
	if len(jobs) != 0 {
		t.Fatalf("persisted jobs = %d, want 0", len(jobs))
	}

	deadline := time.Now().Add(time.Second)
	for {
		_, statErr := os.Stat(jobDir)
		if os.IsNotExist(statErr) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("job directory was not removed: %v", statErr)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestDeleteMissingJob(t *testing.T) {
	manager := &Manager{jobs: map[string]Job{}}
	if err := manager.Delete("missing"); err == nil {
		t.Fatal("Delete missing job returned nil error")
	}
}

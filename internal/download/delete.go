package download

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type activeJobKey struct {
	manager *Manager
	id      string
}

type activeJobState struct {
	cancel context.CancelFunc
	done   chan struct{}
	once   sync.Once
}

func (s *activeJobState) finish() {
	if s == nil {
		return
	}
	s.once.Do(func() { close(s.done) })
}

var activeJobCancels sync.Map

// registerActiveJob closes the race between a queued goroutine starting and a
// user deleting that job. The job must still exist while we register its cancel
// function, otherwise run() aborts before starting external processes.
func (m *Manager) registerActiveJob(id string, cancel context.CancelFunc) (*activeJobState, bool) {
	if m == nil || cancel == nil {
		return nil, false
	}
	state := &activeJobState{cancel: cancel, done: make(chan struct{})}
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.jobs[id]; !ok {
		return nil, false
	}
	activeJobCancels.Store(activeJobKey{manager: m, id: id}, state)
	return state, true
}

func (m *Manager) unregisterActiveJob(id string, state *activeJobState) {
	activeJobCancels.Delete(activeJobKey{manager: m, id: id})
	state.finish()
}

// Delete removes a job from the queue/history, cancels in-flight work and
// removes the job's downloaded/generated files. File cleanup waits briefly for
// external processes to release handles so deleting a stuck job also unblocks
// the rest of the queue.
func (m *Manager) Delete(id string) error {
	if m == nil {
		return fmt.Errorf("job manager is unavailable")
	}
	id = strings.TrimSpace(id)
	if id == "" {
		return fmt.Errorf("job id is required")
	}

	key := activeJobKey{manager: m, id: id}
	var active *activeJobState

	m.mu.Lock()
	if _, ok := m.jobs[id]; !ok {
		m.mu.Unlock()
		return fmt.Errorf("job not found")
	}
	if value, ok := activeJobCancels.LoadAndDelete(key); ok {
		active, _ = value.(*activeJobState)
	}
	delete(m.jobs, id)
	m.mu.Unlock()

	if active != nil && active.cancel != nil {
		active.cancel()
	}

	if m.store != nil {
		if err := m.store.Delete(id); err != nil {
			return err
		}
	}

	jobDir := filepath.Join(m.downloadDir, id)
	cleanup := func() {
		if err := os.RemoveAll(jobDir); err != nil {
			log.Printf("remove deleted job directory %s: %v", jobDir, err)
		}
	}
	if active == nil {
		cleanup()
		return nil
	}

	go func() {
		select {
		case <-active.done:
		case <-time.After(15 * time.Second):
			log.Printf("job %s did not stop within 15s; forcing file cleanup", id)
		}
		cleanup()
	}()
	return nil
}

func (s *jobStore) Delete(id string) error {
	if s == nil || s.db == nil {
		return fmt.Errorf("job database is unavailable")
	}
	if _, err := s.db.Exec("DELETE FROM jobs WHERE id = ?", id); err != nil {
		return fmt.Errorf("delete job %s: %w", id, err)
	}
	return nil
}

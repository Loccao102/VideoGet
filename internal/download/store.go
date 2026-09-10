package download

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/Loccao102/VideoGet/internal/localize"
	"github.com/Loccao102/VideoGet/internal/model"
	_ "modernc.org/sqlite"
)

type jobStore struct {
	db   *sql.DB
	path string
}

func openJobStore(path string) (*jobStore, error) {
	if path == "" {
		return nil, fmt.Errorf("job database path is required")
	}
	if path != ":memory:" {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return nil, fmt.Errorf("create job database directory: %w", err)
		}
	}

	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open job database: %w", err)
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)

	store := &jobStore{db: db, path: path}
	if err := store.init(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return store, nil
}

func (s *jobStore) init() error {
	statements := []string{
		"PRAGMA busy_timeout = 5000",
		"PRAGMA journal_mode = WAL",
		"PRAGMA synchronous = NORMAL",
		`CREATE TABLE IF NOT EXISTS jobs (
			id TEXT PRIMARY KEY,
			status TEXT NOT NULL,
			video_json TEXT NOT NULL,
			source_output TEXT NOT NULL DEFAULT '',
			output TEXT NOT NULL DEFAULT '',
			localization_json TEXT,
			error TEXT NOT NULL DEFAULT '',
			attempts INTEGER NOT NULL DEFAULT 1,
			created_at INTEGER NOT NULL,
			updated_at INTEGER NOT NULL
		)`,
		"CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at DESC)",
	}
	for _, statement := range statements {
		if _, err := s.db.Exec(statement); err != nil {
			return fmt.Errorf("initialize job database: %w", err)
		}
	}
	return nil
}

func (s *jobStore) Upsert(job Job) error {
	videoJSON, err := json.Marshal(job.Video)
	if err != nil {
		return fmt.Errorf("encode job video: %w", err)
	}
	var localizationJSON any
	if job.Localization != nil {
		encoded, err := json.Marshal(job.Localization)
		if err != nil {
			return fmt.Errorf("encode localization result: %w", err)
		}
		localizationJSON = string(encoded)
	}

	_, err = s.db.Exec(`
		INSERT INTO jobs (
			id, status, video_json, source_output, output, localization_json,
			error, attempts, created_at, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET
			status = excluded.status,
			video_json = excluded.video_json,
			source_output = excluded.source_output,
			output = excluded.output,
			localization_json = excluded.localization_json,
			error = excluded.error,
			attempts = excluded.attempts,
			created_at = excluded.created_at,
			updated_at = excluded.updated_at
	`,
		job.ID,
		string(job.Status),
		string(videoJSON),
		job.SourceOutput,
		job.Output,
		localizationJSON,
		job.Error,
		job.Attempts,
		job.CreatedAt.UnixNano(),
		job.UpdatedAt.UnixNano(),
	)
	if err != nil {
		return fmt.Errorf("persist job %s: %w", job.ID, err)
	}
	return nil
}

func (s *jobStore) LoadAll() ([]Job, error) {
	rows, err := s.db.Query(`
		SELECT id, status, video_json, source_output, output, localization_json,
		       error, attempts, created_at, updated_at
		FROM jobs
		ORDER BY created_at DESC
	`)
	if err != nil {
		return nil, fmt.Errorf("load jobs: %w", err)
	}
	defer rows.Close()

	jobs := make([]Job, 0)
	for rows.Next() {
		var (
			job              Job
			status           string
			videoJSON        string
			localizationJSON sql.NullString
			createdAt        int64
			updatedAt        int64
		)
		if err := rows.Scan(
			&job.ID,
			&status,
			&videoJSON,
			&job.SourceOutput,
			&job.Output,
			&localizationJSON,
			&job.Error,
			&job.Attempts,
			&createdAt,
			&updatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan job: %w", err)
		}
		job.Status = JobStatus(status)
		job.CreatedAt = time.Unix(0, createdAt).UTC()
		job.UpdatedAt = time.Unix(0, updatedAt).UTC()
		if err := json.Unmarshal([]byte(videoJSON), &job.Video); err != nil {
			return nil, fmt.Errorf("decode video for job %s: %w", job.ID, err)
		}
		if localizationJSON.Valid && localizationJSON.String != "" {
			var result localize.Result
			if err := json.Unmarshal([]byte(localizationJSON.String), &result); err != nil {
				return nil, fmt.Errorf("decode localization for job %s: %w", job.ID, err)
			}
			job.Localization = &result
		}
		jobs = append(jobs, job)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate jobs: %w", err)
	}
	return jobs, nil
}

func (s *jobStore) Close() error {
	if s == nil || s.db == nil {
		return nil
	}
	return s.db.Close()
}

// Compile-time anchors make accidental package refactors around persisted JSON obvious.
var _ = model.Video{}

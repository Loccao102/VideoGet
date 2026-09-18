package download

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
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
			processing_mode TEXT NOT NULL DEFAULT 'dub',
			output_aspect TEXT NOT NULL DEFAULT 'original',
			rendered_output TEXT NOT NULL DEFAULT '',
			aspect_outputs_json TEXT NOT NULL DEFAULT '{}',
			subtitle_revision INTEGER NOT NULL DEFAULT 0,
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
	if err := s.ensureColumn("processing_mode", "TEXT NOT NULL DEFAULT 'dub'"); err != nil {
		return err
	}
	if err := s.ensureColumn("output_aspect", "TEXT NOT NULL DEFAULT 'original'"); err != nil {
		return err
	}
	if err := s.ensureColumn("rendered_output", "TEXT NOT NULL DEFAULT ''"); err != nil {
		return err
	}
	if err := s.ensureColumn("aspect_outputs_json", "TEXT NOT NULL DEFAULT '{}'"); err != nil {
		return err
	}
	if err := s.ensureColumn("subtitle_revision", "INTEGER NOT NULL DEFAULT 0"); err != nil {
		return err
	}
	return nil
}

func (s *jobStore) ensureColumn(name, definition string) error {
	rows, err := s.db.Query("PRAGMA table_info(jobs)")
	if err != nil {
		return fmt.Errorf("inspect jobs schema: %w", err)
	}
	found := false
	for rows.Next() {
		var cid int
		var columnName, columnType string
		var notNull int
		var defaultValue sql.NullString
		var pk int
		if err := rows.Scan(&cid, &columnName, &columnType, &notNull, &defaultValue, &pk); err != nil {
			_ = rows.Close()
			return fmt.Errorf("scan jobs schema: %w", err)
		}
		if columnName == name {
			found = true
			break
		}
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return fmt.Errorf("iterate jobs schema: %w", err)
	}
	if err := rows.Close(); err != nil {
		return fmt.Errorf("close jobs schema rows: %w", err)
	}
	if found {
		return nil
	}
	if _, err := s.db.Exec("ALTER TABLE jobs ADD COLUMN " + name + " " + definition); err != nil {
		return fmt.Errorf("add jobs.%s: %w", name, err)
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
	aspectOutputsJSON, err := json.Marshal(job.AspectOutputs)
	if err != nil {
		return fmt.Errorf("encode aspect outputs: %w", err)
	}

	_, err = s.db.Exec(`
		INSERT INTO jobs (
			id, status, video_json, source_output, output, localization_json,
			error, attempts, processing_mode, output_aspect, rendered_output, aspect_outputs_json,
			subtitle_revision, created_at, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET
			status = excluded.status,
			video_json = excluded.video_json,
			source_output = excluded.source_output,
			output = excluded.output,
			localization_json = excluded.localization_json,
			error = excluded.error,
			attempts = excluded.attempts,
			processing_mode = excluded.processing_mode,
			output_aspect = excluded.output_aspect,
			rendered_output = excluded.rendered_output,
			aspect_outputs_json = excluded.aspect_outputs_json,
			subtitle_revision = excluded.subtitle_revision,
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
		normalizeProcessingMode(job.ProcessingMode),
		normalizeOutputAspect(job.OutputAspect),
		job.RenderedOutput,
		string(aspectOutputsJSON),
		job.SubtitleRevision,
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
		       error, attempts, processing_mode, output_aspect, rendered_output, aspect_outputs_json,
		       subtitle_revision, created_at, updated_at
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
			aspectOutputsJSON string
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
			&job.ProcessingMode,
			&job.OutputAspect,
			&job.RenderedOutput,
			&aspectOutputsJSON,
			&job.SubtitleRevision,
			&createdAt,
			&updatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan job: %w", err)
		}
		job.Status = JobStatus(status)
		job.ProcessingMode = normalizeProcessingMode(job.ProcessingMode)
		job.OutputAspect = normalizeOutputAspect(job.OutputAspect)
		if strings.TrimSpace(aspectOutputsJSON) != "" {
			if err := json.Unmarshal([]byte(aspectOutputsJSON), &job.AspectOutputs); err != nil {
				return nil, fmt.Errorf("decode aspect outputs for job %s: %w", job.ID, err)
			}
		}
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

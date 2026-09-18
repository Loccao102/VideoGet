package download

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Loccao102/VideoGet/internal/localize"
	"github.com/Loccao102/VideoGet/internal/model"
)

type JobStatus string

const (
	JobQueued             JobStatus = "queued"
	JobDownloading        JobStatus = "downloading"
	JobLocalizing         JobStatus = "localizing"
	JobRendering          JobStatus = "rendering"
	JobDone               JobStatus = "done"
	JobFailed             JobStatus = "failed"
	JobLocalizationFailed JobStatus = "localization_failed"
)

const (
	ProcessingDownload     = "download"
	ProcessingSubtitles    = "subtitles"
	ProcessingDub          = "dub"
	ProcessingOCRMusic     = "ocr_music"
	ProcessingOCRSubtitles = "ocr_subtitles"
)

type Job struct {
	ID               string           `json:"id"`
	Status           JobStatus        `json:"status"`
	Video            model.Video      `json:"video"`
	ProcessingMode   string           `json:"processingMode"`
	OutputAspect     string            `json:"outputAspect,omitempty"`
	AspectOutputs    map[string]string `json:"aspectOutputs,omitempty"`
	RenderedOutput   string            `json:"renderedOutput,omitempty"`
	SubtitleRevision int               `json:"subtitleRevision,omitempty"`
	SourceOutput     string            `json:"sourceOutput,omitempty"`
	Output           string            `json:"output,omitempty"`
	Localization     *localize.Result `json:"localization,omitempty"`
	Error            string           `json:"error,omitempty"`
	Attempts         int              `json:"attempts"`
	CreatedAt        time.Time        `json:"createdAt"`
	UpdatedAt        time.Time        `json:"updatedAt"`
}

type Manager struct {
	mu          sync.RWMutex
	jobs        map[string]Job
	downloadDir string
	localizer   *localize.Processor
	store       *jobStore
	downloadSem chan struct{}
}

func NewManager(downloadDir string) (*Manager, error) {
	if strings.TrimSpace(downloadDir) == "" {
		downloadDir = "downloads"
	}
	if err := os.MkdirAll(downloadDir, 0o755); err != nil {
		return nil, fmt.Errorf("create download directory: %w", err)
	}

	dbPath := strings.TrimSpace(os.Getenv("JOB_DB_PATH"))
	if dbPath == "" {
		dbPath = filepath.Join(downloadDir, "videoget.db")
	}
	store, err := openJobStore(dbPath)
	if err != nil {
		return nil, err
	}

	persisted, err := store.LoadAll()
	if err != nil {
		_ = store.Close()
		return nil, err
	}

	localizer := localize.NewFromEnv()
	m := &Manager{
		jobs:        make(map[string]Job, len(persisted)),
		downloadDir: downloadDir,
		localizer:   localizer,
		store:       store,
		downloadSem: make(chan struct{}, envPositiveInt("DOWNLOAD_CONCURRENCY", 3)),
	}

	resume := make([]string, 0)
	for _, job := range persisted {
		if job.Attempts <= 0 {
			job.Attempts = 1
		}
		job.ProcessingMode = normalizeProcessingMode(job.ProcessingMode)
		job.OutputAspect = normalizeOutputAspect(job.OutputAspect)
		switch job.Status {
		case JobQueued, JobDownloading, JobLocalizing, JobRendering:
			job.Status = JobQueued
			job.UpdatedAt = time.Now().UTC()
			resume = append(resume, job.ID)
			if err := store.Upsert(job); err != nil {
				_ = store.Close()
				return nil, err
			}
		}
		m.jobs[job.ID] = job
	}

	localizer.Prewarm()
	for _, id := range resume {
		go m.run(id)
	}
	if len(resume) > 0 {
		log.Printf("resuming %d interrupted VideoGet job(s) from SQLite", len(resume))
	}
	return m, nil
}

func (m *Manager) Close() error {
	if m == nil {
		return nil
	}
	var firstErr error
	if m.localizer != nil {
		if err := m.localizer.Close(); err != nil {
			firstErr = err
		}
	}
	if m.store != nil {
		if err := m.store.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// Start preserves the legacy behavior for callers that do not specify a mode.
func (m *Manager) Start(video model.Video) (Job, error) {
	return m.StartWithModeAndAspect(video, ProcessingDub, OutputAspectOriginal)
}

func (m *Manager) StartWithMode(video model.Video, mode string) (Job, error) {
	return m.StartWithModeAndAspect(video, mode, OutputAspectOriginal)
}

func (m *Manager) StartWithModeAndAspect(video model.Video, mode, aspect string) (Job, error) {
	if video.URL == "" || video.Platform == "" {
		return Job{}, fmt.Errorf("platform and url are required")
	}
	mode, err := validateProcessingMode(mode)
	if err != nil {
		return Job{}, err
	}
	aspect, err = validateOutputAspect(aspect)
	if err != nil {
		return Job{}, err
	}
	now := time.Now().UTC()
	job := Job{
		ID:             newID(),
		Status:         JobQueued,
		Video:          video,
		ProcessingMode: mode,
		OutputAspect:   aspect,
		Attempts:       1,
		CreatedAt:      now,
		UpdatedAt:      now,
	}
	if err := m.store.Upsert(job); err != nil {
		return Job{}, err
	}
	m.mu.Lock()
	m.jobs[job.ID] = job
	m.mu.Unlock()
	go m.run(job.ID)
	return job, nil
}

func (m *Manager) Retry(id string) (Job, error) {
	m.mu.Lock()
	job, ok := m.jobs[id]
	if !ok {
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job not found")
	}
	switch job.Status {
	case JobQueued, JobDownloading, JobLocalizing, JobRendering:
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job is already running")
	case JobDone:
		m.mu.Unlock()
		return Job{}, fmt.Errorf("job is already complete")
	}
	job.Status = JobQueued
	job.Error = ""
	job.Localization = nil
	job.Attempts++
	job.UpdatedAt = time.Now().UTC()
	m.jobs[id] = job
	m.mu.Unlock()

	if err := m.store.Upsert(job); err != nil {
		return Job{}, err
	}
	go m.run(id)
	return job, nil
}

func (m *Manager) Get(id string) (Job, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	job, ok := m.jobs[id]
	return job, ok
}

func (m *Manager) List() []Job {
	m.mu.RLock()
	out := make([]Job, 0, len(m.jobs))
	for _, job := range m.jobs {
		out = append(out, job)
	}
	m.mu.RUnlock()
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.After(out[j].CreatedAt) })
	return out
}

func (m *Manager) LocalizationStatus() map[string]any {
	if m == nil || m.localizer == nil {
		return map[string]any{"enabled": false}
	}
	return m.localizer.Status()
}

func (m *Manager) PersistenceStatus() map[string]any {
	m.mu.RLock()
	count := len(m.jobs)
	m.mu.RUnlock()
	status := map[string]any{"enabled": m.store != nil, "jobs": count}
	if m.store != nil {
		status["path"] = m.store.path
	}
	return status
}

func (m *Manager) update(id string, fn func(*Job)) {
	m.mu.Lock()
	job, ok := m.jobs[id]
	if !ok {
		m.mu.Unlock()
		return
	}
	fn(&job)
	m.jobs[id] = job
	m.mu.Unlock()
	if err := m.store.Upsert(job); err != nil {
		log.Printf("persist job %s: %v", id, err)
	}
}

func (m *Manager) run(id string) {
	job, ok := m.Get(id)
	if !ok {
		return
	}
	job.ProcessingMode = normalizeProcessingMode(job.ProcessingMode)
	job.OutputAspect = normalizeOutputAspect(job.OutputAspect)

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(envPositiveInt("JOB_TIMEOUT_MINUTES", 180))*time.Minute)
	activeState, active := m.registerActiveJob(id, cancel)
	if !active {
		cancel()
		return
	}
	defer func() {
		cancel()
		m.unregisterActiveJob(id, activeState)
	}()

	jobDir := filepath.Join(m.downloadDir, id)
	if err := os.MkdirAll(jobDir, 0o755); err != nil {
		m.fail(id, JobFailed, fmt.Errorf("create job directory: %w", err))
		return
	}

	sourceOutput := ""
	if reusableMedia(job.SourceOutput) {
		sourceOutput = job.SourceOutput
		log.Printf("job %s reusing downloaded source %s", id, sourceOutput)
	} else {
		m.update(id, func(job *Job) {
			job.Status = JobDownloading
			job.Error = ""
			job.UpdatedAt = time.Now().UTC()
		})

		select {
		case m.downloadSem <- struct{}{}:
		case <-ctx.Done():
			m.fail(id, JobFailed, ctx.Err())
			return
		}
		downloadedOutput, err := m.download(ctx, job.Video, jobDir)
		<-m.downloadSem
		if err != nil {
			m.fail(id, JobFailed, err)
			return
		}
		if strings.TrimSpace(downloadedOutput) == "" {
			m.fail(id, JobFailed, fmt.Errorf("download finished without an output path"))
			return
		}
		sourceOutput = downloadedOutput

		m.update(id, func(job *Job) {
			job.SourceOutput = sourceOutput
			job.Output = sourceOutput
			job.Error = ""
			job.UpdatedAt = time.Now().UTC()
		})
	}

	if job.ProcessingMode == ProcessingDownload {
		finalOutput := sourceOutput
		if job.OutputAspect != OutputAspectOriginal {
			m.update(id, func(current *Job) {
				current.Status = JobRendering
				current.Error = ""
				current.UpdatedAt = time.Now().UTC()
			})
			converted, err := m.applyOutputAspect(ctx, sourceOutput, job.OutputAspect)
			if err != nil {
				m.fail(id, JobFailed, err)
				return
			}
			finalOutput = converted
		}
		m.update(id, func(job *Job) {
			job.Status = JobDone
			job.RenderedOutput = sourceOutput
			job.Output = finalOutput
			job.Error = ""
			job.AspectOutputs = map[string]string{OutputAspectOriginal: sourceOutput}
			if job.OutputAspect != OutputAspectOriginal {
				job.AspectOutputs[job.OutputAspect] = finalOutput
			}
			job.UpdatedAt = time.Now().UTC()
		})
		return
	}

	if m.localizer == nil || !m.localizer.Enabled() {
		m.update(id, func(job *Job) {
			job.Status = JobLocalizationFailed
			job.Error = "localization is disabled but this job requires subtitle processing"
			job.Output = sourceOutput
			job.UpdatedAt = time.Now().UTC()
		})
		return
	}

	m.update(id, func(job *Job) {
		job.Status = JobLocalizing
		job.Error = ""
		job.UpdatedAt = time.Now().UTC()
	})

	result, err := m.localizer.ProcessMode(ctx, sourceOutput, job.ProcessingMode)
	if err != nil {
		m.update(id, func(job *Job) {
			job.Status = JobLocalizationFailed
			job.Error = err.Error()
			job.Output = sourceOutput
			job.UpdatedAt = time.Now().UTC()
		})
		return
	}

	renderedOutput := result.OutputVideo
	finalOutput := renderedOutput
	if job.OutputAspect != OutputAspectOriginal {
		m.update(id, func(current *Job) {
			current.Status = JobRendering
			current.Error = ""
			current.UpdatedAt = time.Now().UTC()
		})
		converted, convertErr := m.applyOutputAspect(ctx, result.OutputVideo, job.OutputAspect)
		if convertErr != nil {
			m.fail(id, JobLocalizationFailed, convertErr)
			return
		}
		finalOutput = converted
		result.OutputVideo = converted
	}

	m.update(id, func(job *Job) {
		job.Status = JobDone
		job.Localization = &result
		job.RenderedOutput = renderedOutput
		job.Error = ""
		job.Output = finalOutput
		job.AspectOutputs = map[string]string{OutputAspectOriginal: renderedOutput}
		if job.OutputAspect != OutputAspectOriginal {
			job.AspectOutputs[job.OutputAspect] = finalOutput
		}
		job.UpdatedAt = time.Now().UTC()
	})
}

func (m *Manager) fail(id string, status JobStatus, err error) {
	m.update(id, func(job *Job) {
		job.Status = status
		job.Error = err.Error()
		job.UpdatedAt = time.Now().UTC()
	})
}

func reusableMedia(path string) bool {
	if strings.TrimSpace(path) == "" {
		return false
	}
	info, err := os.Stat(path)
	if err != nil || info.IsDir() || info.Size() <= 0 {
		return false
	}
	return hasAudioStream(path)
}

func (m *Manager) download(ctx context.Context, video model.Video, outputDir string) (string, error) {
	platform := strings.ToLower(strings.TrimSpace(video.Platform))
	switch platform {
	case "bilibili":
		return m.ytdlpBilibili(ctx, video.URL, outputDir)
	case "douyin":
		return m.douyinBrowserAwareDirect(ctx, video.URL, outputDir)
	default:
		if isPublicDownloadPlatform(platform) {
			return m.ytdlpPublic(ctx, platform, video.URL, outputDir)
		}
		return "", fmt.Errorf("unsupported platform %q", video.Platform)
	}
}

func validateProcessingMode(mode string) (string, error) {
	mode = strings.ToLower(strings.TrimSpace(mode))
	if mode == "" {
		mode = ProcessingDub
	}
	switch mode {
	case ProcessingDownload, ProcessingSubtitles, ProcessingDub, ProcessingOCRMusic, ProcessingOCRSubtitles:
		return mode, nil
	default:
		return "", fmt.Errorf("processing mode must be download, subtitles, dub, ocr_music, or ocr_subtitles")
	}
}

func normalizeProcessingMode(mode string) string {
	mode, err := validateProcessingMode(mode)
	if err != nil {
		return ProcessingDub
	}
	return mode
}

func envPositiveInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}

func newID() string {
	buffer := make([]byte, 8)
	if _, err := rand.Read(buffer); err == nil {
		return hex.EncodeToString(buffer)
	}
	return fmt.Sprintf("%d", time.Now().UnixNano())
}

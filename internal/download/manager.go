package download

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"os/exec"
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
	JobDone               JobStatus = "done"
	JobFailed             JobStatus = "failed"
	JobLocalizationFailed JobStatus = "localization_failed"
)

type Job struct {
	ID           string           `json:"id"`
	Status       JobStatus        `json:"status"`
	Video        model.Video      `json:"video"`
	SourceOutput string           `json:"sourceOutput,omitempty"`
	Output       string           `json:"output,omitempty"`
	Localization *localize.Result `json:"localization,omitempty"`
	Error        string           `json:"error,omitempty"`
	Attempts     int              `json:"attempts"`
	CreatedAt    time.Time        `json:"createdAt"`
	UpdatedAt    time.Time        `json:"updatedAt"`
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
		switch job.Status {
		case JobQueued, JobDownloading, JobLocalizing:
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

func (m *Manager) Start(video model.Video) (Job, error) {
	if video.URL == "" || video.Platform == "" {
		return Job{}, fmt.Errorf("platform and url are required")
	}
	now := time.Now().UTC()
	job := Job{
		ID:        newID(),
		Status:    JobQueued,
		Video:     video,
		Attempts:  1,
		CreatedAt: now,
		UpdatedAt: now,
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
	case JobQueued, JobDownloading, JobLocalizing:
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

	jobDir := filepath.Join(m.downloadDir, id)
	if err := os.MkdirAll(jobDir, 0o755); err != nil {
		m.fail(id, JobFailed, fmt.Errorf("create job directory: %w", err))
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(envPositiveInt("JOB_TIMEOUT_MINUTES", 180))*time.Minute)
	defer cancel()

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
		sourceOutput, err := m.download(ctx, job.Video, jobDir)
		<-m.downloadSem
		if err != nil {
			m.fail(id, JobFailed, err)
			return
		}

		m.update(id, func(job *Job) {
			job.SourceOutput = sourceOutput
			job.Output = sourceOutput
			job.Error = ""
			job.UpdatedAt = time.Now().UTC()
		})
	}

	if m.localizer == nil || !m.localizer.Enabled() {
		m.update(id, func(job *Job) {
			job.Status = JobDone
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

	result, err := m.localizer.Process(ctx, sourceOutput)
	if err != nil {
		m.update(id, func(job *Job) {
			job.Status = JobLocalizationFailed
			job.Error = err.Error()
			job.Output = sourceOutput
			job.UpdatedAt = time.Now().UTC()
		})
		return
	}

	m.update(id, func(job *Job) {
		job.Status = JobDone
		job.Localization = &result
		job.Error = ""
		job.Output = result.OutputVideo
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
	switch strings.ToLower(video.Platform) {
	case "bilibili":
		return m.ytdlpBilibili(ctx, video.URL, outputDir)
	case "douyin":
		return m.douyin(ctx, video.URL, outputDir)
	default:
		return "", fmt.Errorf("unsupported platform %q", video.Platform)
	}
}

func (m *Manager) ytdlpBilibili(ctx context.Context, url, outputDir string) (string, error) {
	bin, err := exec.LookPath("yt-dlp")
	if err != nil {
		return "", fmt.Errorf("yt-dlp is not installed: %w", err)
	}

	template := filepath.Join(outputDir, "%(title).120B [%(id)s].%(ext)s")
	formats := []string{
		"bv*[height<=1080]+ba/b[height<=1080]/b",
		"bv*+ba/b",
		"b[ext=mp4]/b",
	}
	attempts := envPositiveInt("BILIBILI_DOWNLOAD_ATTEMPTS", len(formats))
	if attempts > len(formats) {
		attempts = len(formats)
	}
	cookie := strings.TrimSpace(os.Getenv("BILIBILI_COOKIE"))
	userAgent := strings.TrimSpace(os.Getenv("BILIBILI_USER_AGENT"))
	if userAgent == "" {
		userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
	}

	var errors []string
	for attempt := 0; attempt < attempts; attempt++ {
		cleanupPartialFiles(outputDir)
		args := []string{
			"--ignore-config",
			"--no-playlist",
			"--no-progress",
			"--no-continue",
			"--retries", "3",
			"--fragment-retries", "3",
			"--socket-timeout", "25",
			"--concurrent-fragments", "1",
			"--merge-output-format", "mp4",
			"--format", formats[attempt],
			"--referer", "https://www.bilibili.com/",
			"--user-agent", userAgent,
			"--add-header", "Origin:https://www.bilibili.com",
			"--print", "after_move:filepath",
			"-o", template,
		}
		if cookie != "" {
			args = append(args, "--add-header", "Cookie:"+cookie)
		}
		args = append(args, url)

		cmd := exec.CommandContext(ctx, bin, args...)
		var stdout, stderr bytes.Buffer
		cmd.Stdout = &stdout
		cmd.Stderr = &stderr
		err := cmd.Run()
		if err == nil {
			candidate := resolveYTDLPOutput(stdout.String())
			if candidate != "" {
				if hasAudioStream(candidate) {
					return candidate, nil
				}
				errors = append(errors, fmt.Sprintf("attempt %d downloaded media without audio stream", attempt+1))
			} else {
				errors = append(errors, fmt.Sprintf("attempt %d finished but output file could not be resolved", attempt+1))
			}
		} else {
			message := strings.TrimSpace(stderr.String())
			if message == "" {
				message = err.Error()
			}
			errors = append(errors, fmt.Sprintf("attempt %d format=%q: %s", attempt+1, formats[attempt], message))
		}

		if attempt+1 < attempts {
			wait := time.Duration(3*(1<<attempt)) * time.Second
			select {
			case <-time.After(wait):
			case <-ctx.Done():
				return "", ctx.Err()
			}
		}
	}
	return "", fmt.Errorf("yt-dlp Bilibili download failed after %d strategies: %s", attempts, strings.Join(errors, " | "))
}

func resolveYTDLPOutput(stdout string) string {
	lines := strings.Split(strings.TrimSpace(stdout), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		candidate := strings.TrimSpace(lines[i])
		if candidate == "" {
			continue
		}
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate
		}
	}
	return ""
}

func hasAudioStream(path string) bool {
	bin, err := exec.LookPath("ffprobe")
	if err != nil {
		return true
	}
	cmd := exec.Command(bin,
		"-v", "error",
		"-select_streams", "a:0",
		"-show_entries", "stream=index",
		"-of", "csv=p=0",
		path,
	)
	out, err := cmd.Output()
	return err == nil && strings.TrimSpace(string(out)) != ""
}

func cleanupPartialFiles(root string) {
	_ = filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil || entry.IsDir() {
			return nil
		}
		lower := strings.ToLower(entry.Name())
		if strings.HasSuffix(lower, ".part") || strings.Contains(lower, ".part-") || strings.HasSuffix(lower, ".ytdl") {
			_ = os.Remove(path)
		}
		return nil
	})
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

func (m *Manager) douyin(ctx context.Context, url, outputDir string) (string, error) {
	binary := strings.TrimSpace(os.Getenv("DOUYIN_BIN"))
	if binary == "" {
		binary = "douyin"
	}
	bin, err := exec.LookPath(binary)
	if err != nil {
		return "", fmt.Errorf("douyin-cli is not installed: %w", err)
	}

	startedAt := time.Now().Add(-2 * time.Second)
	cmd := exec.CommandContext(ctx, bin, "-u", url, "-t", "aweme", "-p", outputDir)
	cmd.Env = os.Environ()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("douyin download failed: %s", strings.TrimSpace(string(out)))
	}

	output, err := newestMediaFile(outputDir, startedAt)
	if err != nil {
		return "", fmt.Errorf("douyin download finished but output video was not found: %w", err)
	}
	if !hasAudioStream(output) {
		return "", fmt.Errorf("douyin downloaded media without an audio stream: %s", output)
	}
	return output, nil
}

func newestMediaFile(root string, after time.Time) (string, error) {
	type candidate struct {
		path string
		mod  time.Time
	}
	var files []candidate
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		ext := strings.ToLower(filepath.Ext(path))
		switch ext {
		case ".mp4", ".mkv", ".webm", ".mov", ".m4v":
		default:
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return nil
		}
		if info.ModTime().Before(after) {
			return nil
		}
		files = append(files, candidate{path: path, mod: info.ModTime()})
		return nil
	})
	if err != nil {
		return "", err
	}
	if len(files) == 0 {
		return "", fmt.Errorf("no newly-created media file in %s", root)
	}
	sort.Slice(files, func(i, j int) bool { return files[i].mod.After(files[j].mod) })
	return files[0].path, nil
}

func newID() string {
	buffer := make([]byte, 8)
	if _, err := rand.Read(buffer); err == nil {
		return hex.EncodeToString(buffer)
	}
	return fmt.Sprintf("%d", time.Now().UnixNano())
}

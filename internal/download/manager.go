package download

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
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
	CreatedAt    time.Time        `json:"createdAt"`
	UpdatedAt    time.Time        `json:"updatedAt"`
}

type Manager struct {
	mu          sync.RWMutex
	jobs        map[string]Job
	downloadDir string
	localizer   *localize.Processor
}

func NewManager(downloadDir string) *Manager {
	if strings.TrimSpace(downloadDir) == "" {
		downloadDir = "downloads"
	}
	return &Manager{
		jobs:        map[string]Job{},
		downloadDir: downloadDir,
		localizer:   localize.NewFromEnv(),
	}
}

func (m *Manager) Start(video model.Video) (Job, error) {
	if video.URL == "" || video.Platform == "" {
		return Job{}, fmt.Errorf("platform and url are required")
	}
	if err := os.MkdirAll(m.downloadDir, 0o755); err != nil {
		return Job{}, err
	}
	now := time.Now().UTC()
	job := Job{
		ID:        newID(),
		Status:    JobQueued,
		Video:     video,
		CreatedAt: now,
		UpdatedAt: now,
	}
	m.mu.Lock()
	m.jobs[job.ID] = job
	m.mu.Unlock()
	go m.run(job.ID)
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
	defer m.mu.RUnlock()
	out := make([]Job, 0, len(m.jobs))
	for _, job := range m.jobs {
		out = append(out, job)
	}
	return out
}

func (m *Manager) LocalizationStatus() map[string]any {
	status := map[string]any{"enabled": m.localizer != nil && m.localizer.Enabled()}
	if m.localizer != nil && m.localizer.Enabled() {
		if err := m.localizer.Available(); err != nil {
			status["available"] = false
			status["error"] = err.Error()
		} else {
			status["available"] = true
		}
	}
	return status
}

func (m *Manager) update(id string, fn func(*Job)) {
	m.mu.Lock()
	defer m.mu.Unlock()
	job, ok := m.jobs[id]
	if !ok {
		return
	}
	fn(&job)
	m.jobs[id] = job
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

	m.update(id, func(job *Job) {
		job.Status = JobDownloading
		job.UpdatedAt = time.Now().UTC()
	})

	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Minute)
	defer cancel()

	sourceOutput, err := m.download(ctx, job.Video, jobDir)
	if err != nil {
		m.fail(id, JobFailed, err)
		return
	}

	m.update(id, func(job *Job) {
		job.SourceOutput = sourceOutput
		job.Output = sourceOutput
		job.UpdatedAt = time.Now().UTC()
	})

	if m.localizer == nil || !m.localizer.Enabled() {
		m.update(id, func(job *Job) {
			job.Status = JobDone
			job.UpdatedAt = time.Now().UTC()
		})
		return
	}

	m.update(id, func(job *Job) {
		job.Status = JobLocalizing
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

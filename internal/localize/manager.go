package localize

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Result struct {
	OriginalSubtitle    string             `json:"originalSubtitle,omitempty"`
	VietnameseSubtitle string             `json:"vietnameseSubtitle,omitempty"`
	VoiceTrack          string             `json:"voiceTrack,omitempty"`
	OutputVideo         string             `json:"outputVideo,omitempty"`
	DetectedLanguage    string             `json:"detectedLanguage,omitempty"`
	Segments            int                `json:"segments,omitempty"`
	SkippedReason       string             `json:"skippedReason,omitempty"`
	Timings             map[string]float64 `json:"timings,omitempty"`
	CacheHits           []string           `json:"cacheHits,omitempty"`
	Worker              bool               `json:"worker,omitempty"`
}

type Processor struct {
	enabled bool
	python  string
	script  string
	sem     chan struct{}

	workerEnabled      bool
	workerFallback     bool
	workerPrewarm      bool
	workerScript       string
	workerStartTimeout time.Duration

	workerReqMu sync.Mutex
	workerState sync.RWMutex
	workerCmd   *exec.Cmd
	workerIn    io.WriteCloser
	workerOut   *bufio.Reader
	workerInfo  map[string]any
	workerSince time.Time
	workerStarts int64
	workerRequests int64
}

func NewFromEnv() *Processor {
	enabled := envBool("AUTO_LOCALIZE", true)

	python := strings.TrimSpace(os.Getenv("PYTHON_BIN"))
	if python == "" {
		python = "python3"
	}

	script := strings.TrimSpace(os.Getenv("LOCALIZE_SCRIPT"))
	if script == "" {
		script = firstExisting(
			"/app/scripts/localize_fast.py",
			"/app/scripts/localize.py",
			filepath.FromSlash("scripts/localize_fast.py"),
			filepath.FromSlash("scripts/localize.py"),
		)
		if script == "" {
			script = filepath.FromSlash("scripts/localize_fast.py")
		}
	}

	workerScript := strings.TrimSpace(os.Getenv("LOCALIZE_WORKER_SCRIPT"))
	if workerScript == "" {
		workerScript = firstExisting(
			"/app/scripts/localize_worker.py",
			filepath.FromSlash("scripts/localize_worker.py"),
		)
		if workerScript == "" {
			workerScript = filepath.FromSlash("scripts/localize_worker.py")
		}
	}

	concurrency := envPositiveInt("LOCALIZE_CONCURRENCY", 1)
	startTimeout := time.Duration(envPositiveInt("LOCALIZE_WORKER_START_TIMEOUT_SEC", 600)) * time.Second

	return &Processor{
		enabled:            enabled,
		python:             python,
		script:             script,
		sem:                make(chan struct{}, concurrency),
		workerEnabled:      envBool("LOCALIZE_PERSISTENT_WORKER", true),
		workerFallback:     envBool("LOCALIZE_WORKER_FALLBACK", true),
		workerPrewarm:      envBool("LOCALIZE_WORKER_PREWARM", false),
		workerScript:       workerScript,
		workerStartTimeout: startTimeout,
	}
}

func (p *Processor) Enabled() bool { return p != nil && p.enabled }

func (p *Processor) Available() error {
	if !p.Enabled() {
		return nil
	}
	if _, err := exec.LookPath(p.python); err != nil {
		return fmt.Errorf("python runtime not found: %w", err)
	}
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		return fmt.Errorf("ffmpeg not found: %w", err)
	}
	if _, err := exec.LookPath("ffprobe"); err != nil {
		return fmt.Errorf("ffprobe not found: %w", err)
	}
	if p.workerEnabled {
		if _, err := os.Stat(p.workerScript); err == nil {
			return nil
		} else if !p.workerFallback {
			return fmt.Errorf("persistent localization worker not found at %s: %w", p.workerScript, err)
		}
	}
	if _, err := os.Stat(p.script); err != nil {
		return fmt.Errorf("localization script not found at %s: %w", p.script, err)
	}
	return nil
}

func (p *Processor) Status() map[string]any {
	status := map[string]any{
		"enabled":          p != nil && p.Enabled(),
		"persistentWorker": p != nil && p.workerEnabled,
	}
	if p == nil || !p.Enabled() {
		return status
	}
	if err := p.Available(); err != nil {
		status["available"] = false
		status["error"] = err.Error()
	} else {
		status["available"] = true
	}

	p.workerState.RLock()
	defer p.workerState.RUnlock()
	status["workerRunning"] = p.workerCmd != nil
	status["workerStarts"] = p.workerStarts
	status["workerRequests"] = p.workerRequests
	if !p.workerSince.IsZero() {
		status["workerSince"] = p.workerSince.UTC()
	}
	if len(p.workerInfo) > 0 {
		copyInfo := make(map[string]any, len(p.workerInfo))
		for key, value := range p.workerInfo {
			copyInfo[key] = value
		}
		status["workerInfo"] = copyInfo
	}
	return status
}

// Prewarm starts the persistent worker in the background when explicitly enabled.
// Docker enables this so the Whisper model can load while the user searches/downloads.
func (p *Processor) Prewarm() {
	if p == nil || !p.Enabled() || !p.workerEnabled || !p.workerPrewarm {
		return
	}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), p.workerStartTimeout)
		defer cancel()
		p.workerReqMu.Lock()
		defer p.workerReqMu.Unlock()
		if err := p.ensureWorkerLocked(ctx); err != nil {
			log.Printf("localization worker prewarm failed: %v", err)
		}
	}()
}

func (p *Processor) Close() error {
	if p == nil {
		return nil
	}
	p.workerReqMu.Lock()
	defer p.workerReqMu.Unlock()
	return p.stopWorkerLocked()
}

func (p *Processor) Process(ctx context.Context, input string) (Result, error) {
	if !p.Enabled() {
		return Result{OutputVideo: input}, nil
	}
	if err := p.Available(); err != nil {
		return Result{}, err
	}
	if strings.TrimSpace(input) == "" {
		return Result{}, fmt.Errorf("input video path is required")
	}
	if _, err := os.Stat(input); err != nil {
		return Result{}, fmt.Errorf("input video does not exist: %w", err)
	}

	select {
	case p.sem <- struct{}{}:
		defer func() { <-p.sem }()
	case <-ctx.Done():
		return Result{}, ctx.Err()
	}

	outputDir := filepath.Join(filepath.Dir(input), "localized")
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return Result{}, fmt.Errorf("create localization output directory: %w", err)
	}

	if p.workerEnabled {
		result, err := p.processWorker(ctx, input, outputDir)
		if err == nil {
			return validateResult(result)
		}
		if _, ok := err.(*workerJobError); ok {
			return Result{}, err
		}
		if !p.workerFallback {
			return Result{}, err
		}
		log.Printf("persistent localization worker unavailable, using one-shot fallback: %v", err)
	}

	return p.processScript(ctx, input, outputDir)
}

type workerJobError struct{ message string }

func (e *workerJobError) Error() string { return "localization failed: " + e.message }

type workerRequest struct {
	ID        string `json:"id"`
	Input     string `json:"input"`
	OutputDir string `json:"outputDir"`
}

type workerResponse struct {
	ID     string `json:"id"`
	OK     bool   `json:"ok"`
	Result Result `json:"result"`
	Error  string `json:"error"`
}

func (p *Processor) processWorker(ctx context.Context, input, outputDir string) (Result, error) {
	p.workerReqMu.Lock()
	defer p.workerReqMu.Unlock()

	if err := p.ensureWorkerLocked(ctx); err != nil {
		return Result{}, fmt.Errorf("start persistent localization worker: %w", err)
	}

	request := workerRequest{
		ID:        strconv.FormatInt(time.Now().UnixNano(), 10),
		Input:     input,
		OutputDir: outputDir,
	}
	payload, err := json.Marshal(request)
	if err != nil {
		return Result{}, err
	}

	p.workerState.RLock()
	stdin := p.workerIn
	stdout := p.workerOut
	p.workerState.RUnlock()
	if stdin == nil || stdout == nil {
		_ = p.stopWorkerLocked()
		return Result{}, fmt.Errorf("persistent worker pipes are unavailable")
	}

	if _, err := stdin.Write(append(payload, '\n')); err != nil {
		_ = p.stopWorkerLocked()
		return Result{}, fmt.Errorf("send localization request to worker: %w", err)
	}

	line, err := readLineContext(ctx, stdout)
	if err != nil {
		_ = p.stopWorkerLocked()
		return Result{}, fmt.Errorf("read localization worker response: %w", err)
	}
	var response workerResponse
	if err := json.Unmarshal(bytes.TrimSpace([]byte(line)), &response); err != nil {
		_ = p.stopWorkerLocked()
		return Result{}, fmt.Errorf("decode localization worker response: %w; output=%q", err, strings.TrimSpace(line))
	}
	if response.ID != request.ID {
		_ = p.stopWorkerLocked()
		return Result{}, fmt.Errorf("localization worker protocol mismatch: request=%s response=%s", request.ID, response.ID)
	}

	p.workerState.Lock()
	p.workerRequests++
	p.workerState.Unlock()
	if !response.OK {
		return Result{}, &workerJobError{message: strings.TrimSpace(response.Error)}
	}
	return response.Result, nil
}

func (p *Processor) ensureWorkerLocked(ctx context.Context) error {
	p.workerState.RLock()
	running := p.workerCmd != nil
	p.workerState.RUnlock()
	if running {
		return nil
	}
	if _, err := os.Stat(p.workerScript); err != nil {
		return err
	}

	cmd := exec.Command(p.python, p.workerScript)
	cmd.Env = os.Environ()
	cmd.Stderr = os.Stderr
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		return err
	}
	if err := cmd.Start(); err != nil {
		_ = stdin.Close()
		return err
	}
	reader := bufio.NewReader(stdoutPipe)

	p.workerState.Lock()
	p.workerCmd = cmd
	p.workerIn = stdin
	p.workerOut = reader
	p.workerInfo = nil
	p.workerSince = time.Now().UTC()
	p.workerStarts++
	p.workerState.Unlock()

	startCtx, cancel := context.WithTimeout(ctx, p.workerStartTimeout)
	defer cancel()
	line, err := readLineContext(startCtx, reader)
	if err != nil {
		_ = p.stopWorkerLocked()
		return fmt.Errorf("worker did not become ready: %w", err)
	}
	var ready map[string]any
	if err := json.Unmarshal(bytes.TrimSpace([]byte(line)), &ready); err != nil {
		_ = p.stopWorkerLocked()
		return fmt.Errorf("decode worker ready message: %w; output=%q", err, strings.TrimSpace(line))
	}
	if ready["type"] != "ready" {
		_ = p.stopWorkerLocked()
		return fmt.Errorf("unexpected worker ready message: %q", strings.TrimSpace(line))
	}
	delete(ready, "type")
	p.workerState.Lock()
	p.workerInfo = ready
	p.workerState.Unlock()
	return nil
}

func (p *Processor) stopWorkerLocked() error {
	p.workerState.Lock()
	cmd := p.workerCmd
	stdin := p.workerIn
	p.workerCmd = nil
	p.workerIn = nil
	p.workerOut = nil
	p.workerInfo = nil
	p.workerSince = time.Time{}
	p.workerState.Unlock()

	if stdin != nil {
		_ = stdin.Close()
	}
	if cmd == nil || cmd.Process == nil {
		return nil
	}
	killErr := cmd.Process.Kill()
	waitErr := cmd.Wait()
	if killErr != nil && !strings.Contains(strings.ToLower(killErr.Error()), "process already finished") {
		return killErr
	}
	if waitErr != nil {
		// A killed worker normally returns an exit-status error; it is not a shutdown failure.
		if _, ok := waitErr.(*exec.ExitError); !ok {
			return waitErr
		}
	}
	return nil
}

func readLineContext(ctx context.Context, reader *bufio.Reader) (string, error) {
	type readResult struct {
		line string
		err  error
	}
	result := make(chan readResult, 1)
	go func() {
		line, err := reader.ReadString('\n')
		result <- readResult{line: line, err: err}
	}()
	select {
	case item := <-result:
		return item.line, item.err
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

func (p *Processor) processScript(ctx context.Context, input, outputDir string) (Result, error) {
	cmd := exec.CommandContext(ctx, p.python, p.script,
		"--input", input,
		"--output-dir", outputDir,
	)
	cmd.Env = os.Environ()
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return Result{}, fmt.Errorf("localization failed: %s", message)
	}

	var result Result
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &result); err != nil {
		return Result{}, fmt.Errorf("decode localization result: %w; output=%q", err, strings.TrimSpace(stdout.String()))
	}
	return validateResult(result)
}

func validateResult(result Result) (Result, error) {
	if result.OutputVideo == "" {
		return Result{}, fmt.Errorf("localization finished without output video")
	}
	return result, nil
}

func envBool(name string, fallback bool) bool {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	switch strings.ToLower(value) {
	case "0", "false", "no", "off":
		return false
	case "1", "true", "yes", "on":
		return true
	default:
		return fallback
	}
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

func firstExisting(candidates ...string) string {
	for _, candidate := range candidates {
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
	}
	return ""
}

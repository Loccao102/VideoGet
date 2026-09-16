package download

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

type douyinBrowserMediaPayload struct {
	Candidates  []string `json:"candidates"`
	FinalURL    string   `json:"finalUrl"`
	Title       string   `json:"title"`
	CookieCount int      `json:"cookieCount"`
	Error       string   `json:"error"`
}

// douyinBrowserAwareDirect keeps lightweight HTTP resolvers first, then asks a
// real Chromium page for fresh media URLs through CDP. The old --dump-dom path
// is disabled by default because current Douyin pages often expose media only
// through network traffic/blob-backed video elements.
func (m *Manager) douyinBrowserAwareDirect(ctx context.Context, rawURL, outputDir string) (string, error) {
	output, primaryErr := m.douyinResumableDirect(ctx, rawURL, outputDir)
	if primaryErr == nil {
		return output, nil
	}
	if !envDownloadBool("DOUYIN_BROWSER_FALLBACK", false) {
		return "", primaryErr
	}

	videoID, err := resolveDouyinVideoID(ctx, rawURL)
	if err != nil {
		return "", fmt.Errorf("%v | browser fallback could not resolve video id: %w", primaryErr, err)
	}
	pageURL := "https://www.douyin.com/video/" + videoID

	payload, helperErr := fetchDouyinBrowserMediaPayload(ctx, pageURL)
	if helperErr == nil {
		candidates := normalizeDouyinBrowserCandidates(payload.Candidates)
		if len(candidates) > 0 {
			cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE"))
			if output, failures := tryDouyinCandidates(ctx, candidates, videoID, "browser-cdp", pageURL, outputDir, cookie); output != "" {
				return output, nil
			} else if len(failures) > 0 {
				return "", fmt.Errorf("%v | browser CDP fallback: %s", primaryErr, strings.Join(failures, " | "))
			}
		}
		detail := fmt.Sprintf("rendered %q (%s) but captured no playable media URL", payload.Title, payload.FinalURL)
		if payload.CookieCount == 0 {
			detail += "; no DOUYIN_COOKIE was injected"
		}
		if !envDownloadBool("DOUYIN_BROWSER_DOM_FALLBACK", false) {
			return "", fmt.Errorf("%v | browser CDP fallback: %s", primaryErr, detail)
		}
		helperErr = fmt.Errorf("%s", detail)
	}

	if !envDownloadBool("DOUYIN_BROWSER_DOM_FALLBACK", false) {
		return "", fmt.Errorf("%v | browser CDP fallback: %w", primaryErr, helperErr)
	}

	// Compatibility-only fallback. Keep it opt-in because it is slower and can
	// miss media that current Douyin pages attach through JavaScript/network APIs.
	document, domErr := fetchDouyinBrowserDOM(ctx, pageURL)
	if domErr != nil {
		return "", fmt.Errorf("%v | browser CDP fallback: %v | DOM fallback: %w", primaryErr, helperErr, domErr)
	}
	candidates := douyinMediaCandidates(document)
	if len(candidates) == 0 {
		return "", fmt.Errorf("%v | browser CDP fallback: %v | DOM fallback contained no playable media URL", primaryErr, helperErr)
	}
	cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE"))
	if output, failures := tryDouyinCandidates(ctx, candidates, videoID, "browser-dom", pageURL, outputDir, cookie); output != "" {
		return output, nil
	} else if len(failures) > 0 {
		return "", fmt.Errorf("%v | browser CDP fallback: %v | DOM fallback: %s", primaryErr, helperErr, strings.Join(failures, " | "))
	}
	return "", fmt.Errorf("%v | browser fallback returned no usable media", primaryErr)
}

func fetchDouyinBrowserMediaPayload(ctx context.Context, pageURL string) (douyinBrowserMediaPayload, error) {
	var payload douyinBrowserMediaPayload
	browser, err := findDouyinBrowserBinary()
	if err != nil {
		return payload, err
	}
	python, err := findDouyinBrowserPython()
	if err != nil {
		return payload, err
	}
	script, err := findDouyinVideoBrowserScript()
	if err != nil {
		return payload, err
	}

	timeout := time.Duration(envPositiveInt("DOUYIN_BROWSER_CDP_TIMEOUT_SEC", 25)) * time.Second
	requestCtx, cancel := context.WithTimeout(ctx, timeout+3*time.Second)
	defer cancel()

	cmd := exec.CommandContext(requestCtx, python, script, "--url", pageURL, "--browser-bin", browser)
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		_ = json.Unmarshal(stdout.Bytes(), &payload)
		message := strings.TrimSpace(payload.Error)
		if message == "" {
			message = strings.TrimSpace(stderr.String())
		}
		if len(message) > 1000 {
			message = message[len(message)-1000:]
		}
		if requestCtx.Err() != nil {
			return payload, fmt.Errorf("browser CDP media capture timed out after %s", timeout)
		}
		if message != "" {
			return payload, fmt.Errorf("browser CDP media capture failed: %w: %s", err, message)
		}
		return payload, fmt.Errorf("browser CDP media capture failed: %w", err)
	}
	if err := json.Unmarshal(stdout.Bytes(), &payload); err != nil {
		return payload, fmt.Errorf("decode browser CDP media output: %w", err)
	}
	if strings.TrimSpace(payload.Error) != "" {
		return payload, fmt.Errorf("browser CDP media capture: %s", payload.Error)
	}
	return payload, nil
}

func normalizeDouyinBrowserCandidates(candidates []string) []string {
	out := make([]string, 0, len(candidates))
	seen := make(map[string]struct{}, len(candidates))
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(strings.ReplaceAll(candidate, "&amp;", "&"))
		if strings.HasPrefix(candidate, "http://") {
			candidate = "https://" + strings.TrimPrefix(candidate, "http://")
		}
		lower := strings.ToLower(candidate)
		if !strings.HasPrefix(lower, "https://") {
			continue
		}
		if !(strings.Contains(lower, "douyinvod.com") || strings.Contains(lower, "mime_type=video") || strings.Contains(lower, "/aweme/v1/play/") || strings.Contains(lower, ".mp4")) {
			continue
		}
		if _, ok := seen[candidate]; ok {
			continue
		}
		seen[candidate] = struct{}{}
		out = append(out, candidate)
	}
	return out
}

func findDouyinBrowserPython() (string, error) {
	if configured := strings.TrimSpace(os.Getenv("DOUYIN_BROWSER_PYTHON")); configured != "" {
		if found, err := exec.LookPath(configured); err == nil {
			return found, nil
		}
		return "", fmt.Errorf("DOUYIN_BROWSER_PYTHON %q was not found", configured)
	}
	for _, candidate := range []string{"python3", "python"} {
		if found, err := exec.LookPath(candidate); err == nil {
			return found, nil
		}
	}
	return "", fmt.Errorf("python was not found for Douyin browser media capture")
}

func findDouyinVideoBrowserScript() (string, error) {
	if configured := strings.TrimSpace(os.Getenv("DOUYIN_VIDEO_BROWSER_SCRIPT")); configured != "" {
		if info, err := os.Stat(configured); err == nil && !info.IsDir() {
			return configured, nil
		}
		return "", fmt.Errorf("DOUYIN_VIDEO_BROWSER_SCRIPT %q was not found", configured)
	}
	for _, candidate := range []string{"/app/scripts/douyin_video_browser.py", "scripts/douyin_video_browser.py"} {
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("Douyin browser media helper script was not found")
}

func fetchDouyinBrowserDOM(ctx context.Context, pageURL string) (string, error) {
	browser, err := findDouyinBrowserBinary()
	if err != nil {
		return "", err
	}

	timeout := time.Duration(envPositiveInt("DOUYIN_BROWSER_TIMEOUT_SEC", 25)) * time.Second
	browserCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	profileDir := strings.TrimSpace(os.Getenv("DOUYIN_BROWSER_PROFILE_DIR"))
	cleanupProfile := func() {}
	if profileDir == "" {
		profileDir, err = os.MkdirTemp("", "videoget-douyin-browser-*")
		if err != nil {
			return "", fmt.Errorf("create browser profile: %w", err)
		}
		cleanupProfile = func() { _ = os.RemoveAll(profileDir) }
	}
	defer cleanupProfile()

	args := []string{
		"--headless=new",
		"--disable-gpu",
		"--disable-dev-shm-usage",
		"--no-first-run",
		"--no-default-browser-check",
		"--mute-audio",
		"--hide-scrollbars",
		"--window-size=1280,720",
		fmt.Sprintf("--virtual-time-budget=%d", envPositiveInt("DOUYIN_BROWSER_RENDER_MS", 8000)),
		"--user-data-dir=" + profileDir,
	}
	if envDownloadBool("DOUYIN_BROWSER_NO_SANDBOX", false) {
		args = append(args, "--no-sandbox")
	}
	if extra := strings.TrimSpace(os.Getenv("DOUYIN_BROWSER_EXTRA_ARGS")); extra != "" {
		args = append(args, strings.Fields(extra)...)
	}
	args = append(args, "--dump-dom", pageURL)

	cmd := exec.CommandContext(browserCtx, browser, args...)
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		if browserCtx.Err() != nil {
			return "", fmt.Errorf("browser timed out after %s", timeout)
		}
		message := strings.TrimSpace(stderr.String())
		if len(message) > 600 {
			message = message[len(message)-600:]
		}
		if message != "" {
			return "", fmt.Errorf("browser render failed: %w: %s", err, message)
		}
		return "", fmt.Errorf("browser render failed: %w", err)
	}

	document := stdout.String()
	if strings.TrimSpace(document) == "" {
		return "", fmt.Errorf("browser returned an empty DOM")
	}
	maxBytes := envPositiveInt("DOUYIN_BROWSER_DOM_MAX_MB", 24) * 1024 * 1024
	if len(document) > maxBytes {
		return "", fmt.Errorf("browser DOM exceeded %d MiB safety limit", maxBytes/(1024*1024))
	}
	return document, nil
}

func findDouyinBrowserBinary() (string, error) {
	if configured := strings.TrimSpace(os.Getenv("DOUYIN_BROWSER_BIN")); configured != "" {
		if filepath.IsAbs(configured) {
			info, err := os.Stat(configured)
			if err != nil || info.IsDir() {
				return "", fmt.Errorf("DOUYIN_BROWSER_BIN %q is not an executable file", configured)
			}
			return configured, nil
		}
		if found, err := exec.LookPath(configured); err == nil {
			return found, nil
		}
		return "", fmt.Errorf("DOUYIN_BROWSER_BIN %q was not found", configured)
	}

	for _, candidate := range []string{"chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"} {
		if found, err := exec.LookPath(candidate); err == nil {
			return found, nil
		}
	}
	return "", fmt.Errorf("no Chrome/Chromium binary found; set DOUYIN_BROWSER_BIN")
}

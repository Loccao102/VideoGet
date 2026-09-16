package download

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// douyinBrowserAwareDirect keeps the lightweight HTTP resolver as the primary
// path. A real headless browser is only attempted when explicitly enabled and
// the HTTP resolver has already failed.
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
	document, err := fetchDouyinBrowserDOM(ctx, pageURL)
	if err != nil {
		return "", fmt.Errorf("%v | browser fallback: %w", primaryErr, err)
	}

	candidates := douyinMediaCandidates(document)
	if len(candidates) == 0 {
		return "", fmt.Errorf("%v | browser fallback: rendered DOM contained no playable media URL", primaryErr)
	}

	cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE"))
	if output, failures := tryDouyinCandidates(ctx, candidates, videoID, "browser", pageURL, outputDir, cookie); output != "" {
		return output, nil
	} else if len(failures) > 0 {
		return "", fmt.Errorf("%v | browser fallback: %s", primaryErr, strings.Join(failures, " | "))
	}
	return "", fmt.Errorf("%v | browser fallback returned no usable media", primaryErr)
}

func fetchDouyinBrowserDOM(ctx context.Context, pageURL string) (string, error) {
	browser, err := findDouyinBrowserBinary()
	if err != nil {
		return "", err
	}

	timeout := time.Duration(envPositiveInt("DOUYIN_BROWSER_TIMEOUT_SEC", 45)) * time.Second
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
		fmt.Sprintf("--virtual-time-budget=%d", envPositiveInt("DOUYIN_BROWSER_RENDER_MS", 10000)),
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

	for _, candidate := range []string{
		"chromium",
		"chromium-browser",
		"google-chrome",
		"google-chrome-stable",
		"chrome",
	} {
		if found, err := exec.LookPath(candidate); err == nil {
			return found, nil
		}
	}
	return "", fmt.Errorf("no Chrome/Chromium binary found; set DOUYIN_BROWSER_BIN")
}

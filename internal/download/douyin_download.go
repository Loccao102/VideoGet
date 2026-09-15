package download

import (
	"bytes"
	"context"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var (
	douyinDownloadVideoIDPattern = regexp.MustCompile(`(?i)/video/([0-9]{8,})`)
	douyinVideoSrcPattern        = regexp.MustCompile(`(?is)<video\b[^>]*\bsrc=["']([^"']+)["']`)
	douyinHTTPURLPattern         = regexp.MustCompile(`https?://[^"'<>\\\s]+`)
)

// douyinDirect keeps Douyin isolated from the other download branches. It first
// uses the yt-dlp already shipped by VideoGet, then falls back to resolving a
// playable URL from Douyin's public/share page. No Douyin-specific CLI is used.
func (m *Manager) douyinDirect(ctx context.Context, rawURL, outputDir string) (string, error) {
	var failures []string
	if candidate, err := m.douyinYTDLP(ctx, rawURL, outputDir); err == nil {
		return candidate, nil
	} else {
		failures = append(failures, "yt-dlp: "+err.Error())
	}

	if envDownloadBool("DOUYIN_PAGE_FALLBACK", true) {
		if candidate, err := m.douyinPageFallback(ctx, rawURL, outputDir); err == nil {
			return candidate, nil
		} else {
			failures = append(failures, "page-fallback: "+err.Error())
		}
	}

	return "", fmt.Errorf("Douyin download failed: %s", strings.Join(failures, " | "))
}

func (m *Manager) douyinYTDLP(ctx context.Context, rawURL, outputDir string) (string, error) {
	bin, err := exec.LookPath("yt-dlp")
	if err != nil {
		return "", fmt.Errorf("yt-dlp is not installed: %w", err)
	}

	cookiePath, cleanupCookie, err := writeDouyinCookieFile(strings.TrimSpace(os.Getenv("DOUYIN_COOKIE")))
	if err != nil {
		return "", err
	}
	defer cleanupCookie()

	userAgent := douyinUserAgent()
	template := filepath.Join(outputDir, "douyin-%(id)s.%(ext)s")
	formats := []string{
		"b[ext=mp4]/b",
		"bv*+ba/b",
	}
	attempts := envPositiveInt("DOUYIN_DOWNLOAD_ATTEMPTS", len(formats))
	if attempts > len(formats) {
		attempts = len(formats)
	}

	var failures []string
	for attempt := 0; attempt < attempts; attempt++ {
		cleanupPartialFiles(outputDir)
		args := []string{
			"--ignore-config",
			"--no-playlist",
			"--no-progress",
			"--no-continue",
			"--retries", "3",
			"--fragment-retries", "3",
			"--extractor-retries", "2",
			"--retry-sleep", "2",
			"--socket-timeout", "25",
			"--merge-output-format", "mp4",
			"--format", formats[attempt],
			"--referer", "https://www.douyin.com/",
			"--user-agent", userAgent,
			"--add-header", "Origin:https://www.douyin.com",
			"--print", "after_move:filepath",
			"-o", template,
		}
		if cookiePath != "" {
			args = append(args, "--cookies", cookiePath)
		}
		args = append(args, rawURL)

		cmd := exec.CommandContext(ctx, bin, args...)
		var stdout, stderr bytes.Buffer
		cmd.Stdout = &stdout
		cmd.Stderr = &stderr
		if err := cmd.Run(); err == nil {
			candidate := resolveYTDLPOutput(stdout.String())
			if candidate == "" {
				failures = append(failures, fmt.Sprintf("attempt %d finished but output path was not reported", attempt+1))
				continue
			}
			if !hasAudioStream(candidate) {
				_ = os.Remove(candidate)
				failures = append(failures, fmt.Sprintf("attempt %d produced media without audio", attempt+1))
				continue
			}
			return candidate, nil
		} else {
			message := strings.TrimSpace(stderr.String())
			if message == "" {
				message = err.Error()
			}
			if len(message) > 900 {
				message = message[:900]
			}
			failures = append(failures, fmt.Sprintf("attempt %d format=%q: %s", attempt+1, formats[attempt], message))
		}

		if attempt+1 < attempts {
			select {
			case <-time.After(time.Duration(2*(attempt+1)) * time.Second):
			case <-ctx.Done():
				return "", ctx.Err()
			}
		}
	}
	return "", fmt.Errorf("yt-dlp failed after %d strategy(s): %s", attempts, strings.Join(failures, " | "))
}

func (m *Manager) douyinPageFallback(ctx context.Context, rawURL, outputDir string) (string, error) {
	pages := []string{rawURL}
	if match := douyinDownloadVideoIDPattern.FindStringSubmatch(rawURL); len(match) == 2 {
		pages = append(pages, "https://www.iesdouyin.com/share/video/"+match[1]+"/")
	}

	cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE"))
	client := &http.Client{Timeout: time.Duration(envPositiveInt("DOUYIN_PAGE_TIMEOUT_SEC", 25)) * time.Second}
	var failures []string
	seenPages := map[string]struct{}{}
	for _, pageURL := range pages {
		if _, ok := seenPages[pageURL]; ok {
			continue
		}
		seenPages[pageURL] = struct{}{}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, pageURL, nil)
		if err != nil {
			failures = append(failures, err.Error())
			continue
		}
		req.Header.Set("User-Agent", douyinUserAgent())
		req.Header.Set("Accept", "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
		req.Header.Set("Referer", "https://www.douyin.com/")
		if cookie != "" {
			req.Header.Set("Cookie", cookie)
		}

		resp, err := client.Do(req)
		if err != nil {
			failures = append(failures, fmt.Sprintf("open %s: %v", pageURL, err))
			continue
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 6<<20))
		resp.Body.Close()
		if readErr != nil {
			failures = append(failures, fmt.Sprintf("read %s: %v", pageURL, readErr))
			continue
		}
		if resp.StatusCode < 200 || resp.StatusCode >= 400 {
			failures = append(failures, fmt.Sprintf("open %s: HTTP %d", pageURL, resp.StatusCode))
			continue
		}

		candidates := douyinMediaCandidates(string(body))
		if len(candidates) == 0 {
			failures = append(failures, fmt.Sprintf("open %s: no playable media URL found", pageURL))
			continue
		}
		if len(candidates) > 10 {
			candidates = candidates[:10]
		}
		for index, mediaURL := range candidates {
			output := filepath.Join(outputDir, fmt.Sprintf("douyin-page-fallback-%02d.mp4", index+1))
			if err := downloadDouyinMedia(ctx, mediaURL, pageURL, output, cookie); err != nil {
				failures = append(failures, fmt.Sprintf("candidate %d: %v", index+1, err))
				continue
			}
			if !hasAudioStream(output) {
				_ = os.Remove(output)
				failures = append(failures, fmt.Sprintf("candidate %d: media has no audio", index+1))
				continue
			}
			return output, nil
		}
	}
	if len(failures) == 0 {
		return "", fmt.Errorf("no Douyin page fallback strategy was available")
	}
	return "", fmt.Errorf("%s", strings.Join(failures, " | "))
}

func douyinMediaCandidates(document string) []string {
	normalized := html.UnescapeString(document)
	normalized = strings.NewReplacer(
		`\u002F`, `/`,
		`\u002f`, `/`,
		`\u0026`, `&`,
		`\u003D`, `=`,
		`\u003d`, `=`,
		`\/`, `/`,
	).Replace(normalized)

	seen := map[string]struct{}{}
	results := make([]string, 0, 12)
	appendCandidate := func(candidate string) {
		candidate = strings.TrimSpace(html.UnescapeString(candidate))
		candidate = strings.TrimRight(candidate, `,;)]}`)
		parsed, err := url.Parse(candidate)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" {
			return
		}
		lower := strings.ToLower(candidate)
		if !strings.Contains(lower, ".mp4") &&
			!strings.Contains(lower, "douyinvod") &&
			!strings.Contains(lower, "mime_type=video") &&
			!strings.Contains(lower, "/video/") &&
			!strings.Contains(lower, "/play/") &&
			!strings.Contains(lower, "playwm") {
			return
		}
		if _, ok := seen[candidate]; ok {
			return
		}
		seen[candidate] = struct{}{}
		results = append(results, candidate)
	}

	for _, match := range douyinVideoSrcPattern.FindAllStringSubmatch(normalized, 12) {
		if len(match) == 2 {
			appendCandidate(match[1])
		}
	}
	for _, candidate := range douyinHTTPURLPattern.FindAllString(normalized, 80) {
		appendCandidate(candidate)
	}
	return results
}

func downloadDouyinMedia(ctx context.Context, rawURL, referer, output, cookie string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", douyinUserAgent())
	req.Header.Set("Accept", "video/*,application/octet-stream;q=0.9,*/*;q=0.5")
	req.Header.Set("Referer", referer)
	req.Header.Set("Origin", "https://www.douyin.com")
	if cookie != "" {
		req.Header.Set("Cookie", cookie)
	}

	client := &http.Client{Timeout: time.Duration(envPositiveInt("DOUYIN_MEDIA_TIMEOUT_SEC", 120)) * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	contentType := strings.ToLower(resp.Header.Get("Content-Type"))
	if strings.Contains(contentType, "text/html") || strings.Contains(contentType, "application/json") {
		return fmt.Errorf("unexpected content-type %q", contentType)
	}

	partial := output + ".part"
	_ = os.Remove(partial)
	file, err := os.Create(partial)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(file, resp.Body)
	closeErr := file.Close()
	if copyErr != nil {
		_ = os.Remove(partial)
		return copyErr
	}
	if closeErr != nil {
		_ = os.Remove(partial)
		return closeErr
	}
	info, err := os.Stat(partial)
	if err != nil || info.Size() < 64*1024 {
		_ = os.Remove(partial)
		return fmt.Errorf("downloaded payload is too small to be a video")
	}
	_ = os.Remove(output)
	if err := os.Rename(partial, output); err != nil {
		_ = os.Remove(partial)
		return err
	}
	return nil
}

func writeDouyinCookieFile(rawCookie string) (string, func(), error) {
	cleanup := func() {}
	if strings.TrimSpace(rawCookie) == "" {
		return "", cleanup, nil
	}

	file, err := os.CreateTemp("", "videoget-douyin-cookies-*.txt")
	if err != nil {
		return "", cleanup, fmt.Errorf("create temporary Douyin cookie file: %w", err)
	}
	path := file.Name()
	cleanup = func() { _ = os.Remove(path) }

	if _, err := fmt.Fprintln(file, "# Netscape HTTP Cookie File"); err != nil {
		file.Close()
		cleanup()
		return "", func() {}, err
	}
	count := 0
	for _, part := range strings.Split(rawCookie, ";") {
		pair := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(pair) != 2 {
			continue
		}
		name := strings.TrimSpace(pair[0])
		value := strings.TrimSpace(pair[1])
		if name == "" || strings.ContainsAny(name, "\t\r\n") || strings.ContainsAny(value, "\t\r\n") {
			continue
		}
		if _, err := fmt.Fprintf(file, ".douyin.com\tTRUE\t/\tTRUE\t0\t%s\t%s\n", name, value); err != nil {
			file.Close()
			cleanup()
			return "", func() {}, err
		}
		count++
	}
	if err := file.Close(); err != nil {
		cleanup()
		return "", func() {}, err
	}
	if count == 0 {
		cleanup()
		return "", func() {}, fmt.Errorf("DOUYIN_COOKIE did not contain any valid name=value pairs")
	}
	return path, cleanup, nil
}

func douyinUserAgent() string {
	if value := strings.TrimSpace(os.Getenv("DOUYIN_USER_AGENT")); value != "" {
		return value
	}
	if value := strings.TrimSpace(os.Getenv("DOUYIN_GUEST_USER_AGENT")); value != "" {
		return value
	}
	return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
}

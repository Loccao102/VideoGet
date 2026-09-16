package source

import (
	"bytes"
	"context"
	"fmt"
	"html"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

var (
	douyinNativeSearchMu = &sync.Mutex{}

	douyinSearchAnchorPattern = regexp.MustCompile(`(?is)<a\b[^>]*href\s*=\s*["'][^"']*(?:https?://www\.douyin\.com)?/video/([0-9]{8,})[^"']*["'][^>]*>(.*?)</a>`)
	douyinSearchIDPattern     = regexp.MustCompile(`(?i)(?:https?://www\.douyin\.com)?/video/([0-9]{8,})`)
	douyinAwemeIDPattern      = regexp.MustCompile(`(?i)["'](?:aweme_id|awemeId)["']\s*[:=]\s*["']([0-9]{8,})["']`)
	douyinHTMLTagPattern      = regexp.MustCompile(`(?is)<[^>]+>`)
	douyinDescPattern         = regexp.MustCompile(`(?s)["']desc["']\s*:\s*"((?:\\.|[^"\\])*)"`)
	douyinNicknamePattern     = regexp.MustCompile(`(?s)["']nickname["']\s*:\s*"((?:\\.|[^"\\])*)"`)
	douyinPlayCountPattern    = regexp.MustCompile(`(?i)["']play_count["']\s*:\s*([0-9]+)`)
	douyinDiggCountPattern    = regexp.MustCompile(`(?i)["']digg_count["']\s*:\s*([0-9]+)`)
	douyinCommentPattern      = regexp.MustCompile(`(?i)["']comment_count["']\s*:\s*([0-9]+)`)
	douyinSharePattern        = regexp.MustCompile(`(?i)["']share_count["']\s*:\s*([0-9]+)`)
	douyinCreateTimePattern   = regexp.MustCompile(`(?i)["']create_time["']\s*:\s*([0-9]+)`)
	douyinDurationPattern     = regexp.MustCompile(`(?i)["']duration["']\s*:\s*([0-9]+)`)
)

func (p *DouyinProvider) searchNativeBrowser(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	if limit <= 0 {
		limit = 10
	}
	if limit > 50 {
		limit = 50
	}

	document, err := fetchDouyinSearchDOM(ctx, keyword)
	if err != nil {
		return nil, err
	}
	results := parseDouyinSearchDOM(document, keyword, limit)
	if len(results) == 0 {
		return nil, fmt.Errorf("native Douyin browser search returned no video results for %q", keyword)
	}
	return results, nil
}

func fetchDouyinSearchDOM(ctx context.Context, keyword string) (string, error) {
	browser, err := findDouyinSearchBrowserBinary()
	if err != nil {
		return "", err
	}

	// Chrome profiles do not support concurrent writers reliably. Serializing only
	// the native fallback keeps expanded-keyword searches predictable and avoids
	// profile lock errors while the public-index fast path remains concurrent.
	douyinNativeSearchMu.Lock()
	defer douyinNativeSearchMu.Unlock()

	timeout := envSeconds("DOUYIN_NATIVE_SEARCH_TIMEOUT_SEC", 45)
	browserCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	profileDir := strings.TrimSpace(firstNonEmpty(
		os.Getenv("DOUYIN_NATIVE_SEARCH_PROFILE_DIR"),
		os.Getenv("DOUYIN_BROWSER_PROFILE_DIR"),
	))
	cleanupProfile := func() {}
	if profileDir == "" {
		profileDir, err = os.MkdirTemp("", "videoget-douyin-search-*")
		if err != nil {
			return "", fmt.Errorf("create native search browser profile: %w", err)
		}
		cleanupProfile = func() { _ = os.RemoveAll(profileDir) }
	}
	defer cleanupProfile()

	pageURL := "https://www.douyin.com/search/" + url.PathEscape(keyword) + "?type=general"
	args := []string{
		"--headless=new",
		"--disable-gpu",
		"--disable-dev-shm-usage",
		"--no-first-run",
		"--no-default-browser-check",
		"--mute-audio",
		"--hide-scrollbars",
		"--window-size=1440,1600",
		fmt.Sprintf("--virtual-time-budget=%d", envPositiveSourceInt("DOUYIN_NATIVE_SEARCH_RENDER_MS", 12000)),
		"--user-data-dir=" + profileDir,
	}
	if envSourceBool("DOUYIN_BROWSER_NO_SANDBOX", false) {
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
			return "", fmt.Errorf("native Douyin browser search timed out after %s", timeout)
		}
		message := strings.TrimSpace(stderr.String())
		if len(message) > 700 {
			message = message[len(message)-700:]
		}
		if message != "" {
			return "", fmt.Errorf("native Douyin browser search failed: %w: %s", err, message)
		}
		return "", fmt.Errorf("native Douyin browser search failed: %w", err)
	}

	document := stdout.String()
	if strings.TrimSpace(document) == "" {
		return "", fmt.Errorf("native Douyin browser search returned an empty DOM")
	}
	maxBytes := envPositiveSourceInt("DOUYIN_NATIVE_SEARCH_DOM_MAX_MB", 32) * 1024 * 1024
	if len(document) > maxBytes {
		return "", fmt.Errorf("native Douyin search DOM exceeded %d MiB safety limit", maxBytes/(1024*1024))
	}
	return document, nil
}

func parseDouyinSearchDOM(document, keyword string, limit int) []model.Video {
	if limit <= 0 {
		limit = 10
	}
	normalized := normalizeDouyinSearchDocument(document)
	seen := make(map[string]struct{})
	results := make([]model.Video, 0, limit)
	titles := make(map[string]string)

	for _, match := range douyinSearchAnchorPattern.FindAllStringSubmatch(normalized, -1) {
		if len(match) != 3 {
			continue
		}
		id := strings.TrimSpace(match[1])
		if id == "" {
			continue
		}
		if title := cleanDouyinBrowserText(match[2]); title != "" {
			titles[id] = title
		}
		appendDouyinNativeResult(&results, seen, id, keyword, titles[id], normalized, limit)
		if len(results) >= limit {
			return results
		}
	}

	for _, pattern := range []*regexp.Regexp{douyinSearchIDPattern, douyinAwemeIDPattern} {
		for _, match := range pattern.FindAllStringSubmatch(normalized, -1) {
			if len(match) != 2 {
				continue
			}
			appendDouyinNativeResult(&results, seen, match[1], keyword, titles[match[1]], normalized, limit)
			if len(results) >= limit {
				return results
			}
		}
	}
	return results
}

func appendDouyinNativeResult(results *[]model.Video, seen map[string]struct{}, id, keyword, title, document string, limit int) {
	id = strings.TrimSpace(id)
	if id == "" || len(*results) >= limit {
		return
	}
	if _, exists := seen[id]; exists {
		return
	}
	seen[id] = struct{}{}

	video := model.Video{
		ID:           id,
		Platform:     "douyin",
		URL:          "https://www.douyin.com/video/" + id,
		SearchSource: keyword,
		MediaType:    "video",
	}
	enrichDouyinNativeResult(&video, document)
	if strings.TrimSpace(title) != "" {
		video.Title = title
	}
	if strings.TrimSpace(video.Title) == "" {
		video.Title = "Douyin search: " + keyword
	}
	*results = append(*results, video)
}

func enrichDouyinNativeResult(video *model.Video, document string) {
	if video == nil || video.ID == "" {
		return
	}
	index := strings.Index(document, video.ID)
	if index < 0 {
		return
	}
	start := index - 3000
	if start < 0 {
		start = 0
	}
	end := index + 14000
	if end > len(document) {
		end = len(document)
	}
	window := document[start:end]

	if value := firstDouyinJSONText(douyinDescPattern, window); value != "" {
		video.Title = value
	}
	if value := firstDouyinJSONText(douyinNicknamePattern, window); value != "" {
		video.Author = value
	}
	video.Views = firstDouyinJSONInt(douyinPlayCountPattern, window)
	video.Likes = firstDouyinJSONInt(douyinDiggCountPattern, window)
	video.Comments = firstDouyinJSONInt(douyinCommentPattern, window)
	video.Shares = firstDouyinJSONInt(douyinSharePattern, window)
	if seconds := firstDouyinJSONInt(douyinCreateTimePattern, window); seconds > 0 {
		published := time.Unix(seconds, 0).UTC()
		video.PublishedAt = &published
	}
	if duration := firstDouyinJSONInt(douyinDurationPattern, window); duration > 0 {
		if duration >= 1000 {
			duration /= 1000
		}
		video.DurationSec = duration
	}
}

func normalizeDouyinSearchDocument(document string) string {
	document = html.UnescapeString(document)
	return strings.NewReplacer(
		`\u002F`, `/`,
		`\u002f`, `/`,
		`\u003A`, `:`,
		`\u003a`, `:`,
		`\u0026`, `&`,
		`\u003D`, `=`,
		`\u003d`, `=`,
		`\/`, `/`,
	).Replace(document)
}

func cleanDouyinBrowserText(value string) string {
	value = douyinHTMLTagPattern.ReplaceAllString(value, " ")
	value = html.UnescapeString(value)
	return strings.Join(strings.Fields(value), " ")
}

func firstDouyinJSONText(pattern *regexp.Regexp, value string) string {
	match := pattern.FindStringSubmatch(value)
	if len(match) != 2 {
		return ""
	}
	decoded, err := strconv.Unquote(`"` + match[1] + `"`)
	if err != nil {
		decoded = match[1]
	}
	decoded = html.UnescapeString(decoded)
	return strings.TrimSpace(decoded)
}

func firstDouyinJSONInt(pattern *regexp.Regexp, value string) int64 {
	match := pattern.FindStringSubmatch(value)
	if len(match) != 2 {
		return 0
	}
	parsed, _ := strconv.ParseInt(match[1], 10, 64)
	return parsed
}

func findDouyinSearchBrowserBinary() (string, error) {
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
	return "", fmt.Errorf("no Chrome/Chromium binary found for native Douyin search; set DOUYIN_BROWSER_BIN")
}

func envPositiveSourceInt(name string, fallback int) int {
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

func envSourceBool(name string, fallback bool) bool {
	value := strings.TrimSpace(strings.ToLower(os.Getenv(name)))
	if value == "" {
		return fallback
	}
	switch value {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func douyinNativeSearchEnabled() bool {
	return envSourceBool("DOUYIN_NATIVE_SEARCH", true)
}

func mergeDouyinSearchResults(primary, fallback []model.Video, limit int) []model.Video {
	if limit <= 0 {
		limit = 10
	}
	seen := make(map[string]struct{}, len(primary)+len(fallback))
	merged := make([]model.Video, 0, limit)
	appendItems := func(items []model.Video) {
		for _, item := range items {
			if len(merged) >= limit {
				return
			}
			key := strings.TrimSpace(item.ID)
			if key == "" {
				key = strings.TrimSpace(item.URL)
			}
			if key == "" {
				continue
			}
			if _, exists := seen[key]; exists {
				continue
			}
			seen[key] = struct{}{}
			merged = append(merged, item)
		}
	}
	appendItems(primary)
	appendItems(fallback)
	return merged
}

package source

import (
	"bytes"
	"context"
	"encoding/json"
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

type douyinBrowserSearchPayload struct {
	APIBodies          []string `json:"apiBodies"`
	VideoLinks         []string `json:"videoLinks"`
	DOM                string   `json:"dom"`
	FinalURL           string   `json:"finalUrl"`
	Title              string   `json:"title"`
	CookieCount        int      `json:"cookieCount"`
	CookieNames        []string `json:"cookieNames"`
	CapturedSearchURLs []string `json:"capturedSearchUrls"`
	ProfilePersistent  bool     `json:"profilePersistent"`
	Error              string   `json:"error"`
}

type douyinNativeSearchResponse struct {
	StatusCode int    `json:"status_code"`
	StatusMsg  string `json:"status_msg"`
	Data       []struct {
		Type      int                `json:"type"`
		AwemeInfo *douyinNativeAweme `json:"aweme_info"`
	} `json:"data"`
}

type douyinNativeAweme struct {
	AwemeID    string `json:"aweme_id"`
	Desc       string `json:"desc"`
	CreateTime int64  `json:"create_time"`
	Author     struct {
		Nickname string `json:"nickname"`
	} `json:"author"`
	Statistics struct {
		PlayCount    int64 `json:"play_count"`
		DiggCount    int64 `json:"digg_count"`
		CommentCount int64 `json:"comment_count"`
		ShareCount   int64 `json:"share_count"`
	} `json:"statistics"`
	Video struct {
		Duration int64 `json:"duration"`
		Cover    struct {
			URLList []string `json:"url_list"`
		} `json:"cover"`
	} `json:"video"`
}

func (p *DouyinProvider) searchNativeBrowser(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	if limit <= 0 {
		limit = 10
	}
	if limit > 50 {
		limit = 50
	}

	payload, helperErr := fetchDouyinSearchPayload(ctx, keyword)
	if helperErr == nil {
		results, apiErr := parseDouyinSearchAPIBodies(payload.APIBodies, keyword, limit)
		if len(results) > 0 {
			return results, nil
		}
		// Endpoint/status handling changes frequently. Even when one captured API
		// response reports an error, prefer rendered links/DOM if the page itself
		// successfully exposed video results.
		if len(payload.VideoLinks) > 0 {
			results = parseDouyinVideoLinks(payload.VideoLinks, keyword, limit)
			if len(results) > 0 {
				return results, nil
			}
		}
		if payload.DOM != "" {
			results = parseDouyinSearchDOM(payload.DOM, keyword, limit)
			if len(results) > 0 {
				return results, nil
			}
		}
		if apiErr != nil {
			return nil, apiErr
		}
		if payload.CookieCount == 0 {
			return nil, fmt.Errorf("native Douyin search has no browser cookies; provide DOUYIN_COOKIE once or use a persistent DOUYIN_NATIVE_SEARCH_PROFILE_DIR")
		}
		return nil, fmt.Errorf(
			"native Douyin search rendered %q (%s) but found no videos for %q (cookies=%d, captured_search_responses=%d, persistent_profile=%t)",
			payload.Title,
			payload.FinalURL,
			keyword,
			payload.CookieCount,
			len(payload.CapturedSearchURLs),
			payload.ProfilePersistent,
		)
	}

	// Keep the older DOM-only path as a compatibility fallback for local installs
	// that have Chromium but do not have the Python/aiohttp helper available.
	document, domErr := fetchDouyinSearchDOM(ctx, keyword)
	if domErr == nil {
		results := parseDouyinSearchDOM(document, keyword, limit)
		if len(results) > 0 {
			return results, nil
		}
	}
	if domErr != nil {
		return nil, fmt.Errorf("native Douyin CDP search failed: %v | DOM fallback failed: %v", helperErr, domErr)
	}
	return nil, fmt.Errorf("native Douyin CDP search failed: %v | DOM fallback returned no video results", helperErr)
}

func fetchDouyinSearchPayload(ctx context.Context, keyword string) (douyinBrowserSearchPayload, error) {
	var payload douyinBrowserSearchPayload
	browser, err := findDouyinSearchBrowserBinary()
	if err != nil {
		return payload, err
	}
	python, err := findDouyinSearchPython()
	if err != nil {
		return payload, err
	}
	script, err := findDouyinSearchScript()
	if err != nil {
		return payload, err
	}

	douyinNativeSearchMu.Lock()
	defer douyinNativeSearchMu.Unlock()

	timeout := envSeconds("DOUYIN_NATIVE_SEARCH_TIMEOUT_SEC", 45)
	requestCtx, cancel := context.WithTimeout(ctx, timeout+5*time.Second)
	defer cancel()

	cmd := exec.CommandContext(requestCtx, python, script, "--keyword", keyword, "--browser-bin", browser)
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
			return payload, fmt.Errorf("native Douyin CDP helper timed out after %s", timeout)
		}
		if message != "" {
			return payload, fmt.Errorf("native Douyin CDP helper failed: %w: %s", err, message)
		}
		return payload, fmt.Errorf("native Douyin CDP helper failed: %w", err)
	}
	if err := json.Unmarshal(stdout.Bytes(), &payload); err != nil {
		return payload, fmt.Errorf("decode native Douyin CDP helper output: %w", err)
	}
	if strings.TrimSpace(payload.Error) != "" {
		return payload, fmt.Errorf("native Douyin CDP helper: %s", payload.Error)
	}
	return payload, nil
}

func parseDouyinSearchAPIBodies(bodies []string, keyword string, limit int) ([]model.Video, error) {
	if limit <= 0 {
		limit = 10
	}
	seen := make(map[string]struct{})
	results := make([]model.Video, 0, limit)
	loginRequired := false
	var statusErrors []string

	for _, body := range bodies {
		body = strings.TrimSpace(body)
		if body == "" {
			continue
		}

		var root any
		if err := json.Unmarshal([]byte(body), &root); err != nil {
			continue
		}
		if object, ok := root.(map[string]any); ok {
			if status, ok := douyinJSONInt64(object["status_code"]); ok {
				switch status {
				case 0:
					// Successful response; continue into recursive item extraction.
				case 2483:
					loginRequired = true
				default:
					message, _ := object["status_msg"].(string)
					statusErrors = append(statusErrors, fmt.Sprintf("status %d: %s", status, strings.TrimSpace(message)))
				}
			}
		}

		awemes := make([]douyinNativeAweme, 0, 16)
		collectDouyinNativeAwemes(root, &awemes)
		for i := range awemes {
			appendDouyinAwemeResult(&results, seen, &awemes[i], keyword, limit)
			if len(results) >= limit {
				return results, nil
			}
		}
	}

	if len(results) > 0 {
		return results, nil
	}
	if loginRequired {
		return nil, fmt.Errorf("Douyin native search requires a logged-in browser session (status 2483: 请先登录，再继续搜索吧)")
	}
	if len(statusErrors) > 0 {
		return nil, fmt.Errorf("Douyin native search API returned %s", strings.Join(statusErrors, " | "))
	}
	return nil, nil
}

func collectDouyinNativeAwemes(value any, results *[]douyinNativeAweme) {
	switch node := value.(type) {
	case map[string]any:
		if raw, ok := node["aweme_info"]; ok {
			appendDecodedDouyinAweme(results, raw)
		}
		if raw, ok := node["aweme"]; ok {
			appendDecodedDouyinAweme(results, raw)
		}
		if _, ok := node["aweme_id"]; ok {
			appendDecodedDouyinAweme(results, node)
		}
		for key, child := range node {
			if key == "aweme_info" || key == "aweme" {
				continue
			}
			collectDouyinNativeAwemes(child, results)
		}
	case []any:
		for _, child := range node {
			collectDouyinNativeAwemes(child, results)
		}
	}
}

func appendDecodedDouyinAweme(results *[]douyinNativeAweme, value any) {
	data, err := json.Marshal(value)
	if err != nil {
		return
	}
	var aweme douyinNativeAweme
	if err := json.Unmarshal(data, &aweme); err != nil {
		return
	}
	if strings.TrimSpace(aweme.AwemeID) == "" {
		return
	}
	*results = append(*results, aweme)
}

func appendDouyinAwemeResult(results *[]model.Video, seen map[string]struct{}, aweme *douyinNativeAweme, keyword string, limit int) {
	if aweme == nil || len(*results) >= limit {
		return
	}
	id := strings.TrimSpace(aweme.AwemeID)
	if id == "" {
		return
	}
	if _, exists := seen[id]; exists {
		return
	}
	seen[id] = struct{}{}

	video := model.Video{
		ID:           id,
		Platform:     "douyin",
		Title:        strings.TrimSpace(aweme.Desc),
		Author:       strings.TrimSpace(aweme.Author.Nickname),
		URL:          "https://www.douyin.com/video/" + id,
		MediaType:    "video",
		Views:        aweme.Statistics.PlayCount,
		Likes:        aweme.Statistics.DiggCount,
		Comments:     aweme.Statistics.CommentCount,
		Shares:       aweme.Statistics.ShareCount,
		SearchSource: keyword,
	}
	if video.Title == "" {
		video.Title = "Douyin search: " + keyword
	}
	if aweme.CreateTime > 0 {
		published := time.Unix(aweme.CreateTime, 0).UTC()
		video.PublishedAt = &published
	}
	if aweme.Video.Duration > 0 {
		duration := aweme.Video.Duration
		if duration >= 1000 {
			duration /= 1000
		}
		video.DurationSec = duration
	}
	if len(aweme.Video.Cover.URLList) > 0 {
		video.Thumbnail = aweme.Video.Cover.URLList[0]
	}
	*results = append(*results, video)
}

func douyinJSONInt64(value any) (int64, bool) {
	switch typed := value.(type) {
	case float64:
		return int64(typed), true
	case float32:
		return int64(typed), true
	case int:
		return int64(typed), true
	case int64:
		return typed, true
	case json.Number:
		parsed, err := typed.Int64()
		return parsed, err == nil
	case string:
		parsed, err := strconv.ParseInt(strings.TrimSpace(typed), 10, 64)
		return parsed, err == nil
	default:
		return 0, false
	}
}

func parseDouyinVideoLinks(links []string, keyword string, limit int) []model.Video {
	if limit <= 0 {
		limit = 10
	}
	seen := make(map[string]struct{}, len(links))
	results := make([]model.Video, 0, minInt(limit, len(links)))
	for _, link := range links {
		id := extractDouyinVideoID(strings.TrimSpace(link))
		if id == "" {
			continue
		}
		if _, exists := seen[id]; exists {
			continue
		}
		seen[id] = struct{}{}
		results = append(results, model.Video{
			ID:           id,
			Platform:     "douyin",
			Title:        "Douyin search: " + keyword,
			URL:          "https://www.douyin.com/video/" + id,
			MediaType:    "video",
			SearchSource: keyword,
		})
		if len(results) >= limit {
			break
		}
	}
	return results
}

func findDouyinSearchPython() (string, error) {
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
	return "", fmt.Errorf("python was not found for native Douyin CDP search")
}

func findDouyinSearchScript() (string, error) {
	if configured := strings.TrimSpace(os.Getenv("DOUYIN_NATIVE_SEARCH_SCRIPT")); configured != "" {
		if info, err := os.Stat(configured); err == nil && !info.IsDir() {
			return configured, nil
		}
		return "", fmt.Errorf("DOUYIN_NATIVE_SEARCH_SCRIPT %q was not found", configured)
	}
	for _, candidate := range []string{"/app/scripts/douyin_search_browser.py", "scripts/douyin_search_browser.py"} {
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("native Douyin CDP helper script was not found")
}

func fetchDouyinSearchDOM(ctx context.Context, keyword string) (string, error) {
	browser, err := findDouyinSearchBrowserBinary()
	if err != nil {
		return "", err
	}

	// Chrome profiles do not support concurrent writers reliably. The CDP path
	// serializes separately; this lock remains for the compatibility fallback.
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

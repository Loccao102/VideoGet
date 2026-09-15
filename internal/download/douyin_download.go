package download

import (
	"context"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

var (
	douyinDownloadVideoIDPattern = regexp.MustCompile(`(?i)/(?:share/)?video/([0-9]{8,})`)
	douyinModalIDPattern         = regexp.MustCompile(`(?i)(?:[?&]|\b)modal_id=([0-9]{8,})`)
	douyinRouterDataPattern      = regexp.MustCompile(`window\._ROUTER_DATA\s*=\s*`)
	douyinVideoSrcPattern        = regexp.MustCompile(`(?is)<video\b[^>]*\bsrc=["']([^"']+)["']`)
	douyinHTTPURLPattern         = regexp.MustCompile(`https?://[^"'<>\\\s]+`)
)

type douyinScoredURL struct {
	URL     string
	Bitrate int64
}

// douyinDirect intentionally avoids both douyin-cli and yt-dlp.
// Current server-side path (2026):
//   1. resolve aweme id
//   2. request iesdouyin.com/share/video/{id} with an iPhone UA
//   3. parse window._ROUTER_DATA -> videoInfoRes.item_list[0]
//   4. download play_addr / bit_rate URL directly, with aweme.snssdk.com
//      play URLs as a fallback when only the internal video URI is available.
func (m *Manager) douyinDirect(ctx context.Context, rawURL, outputDir string) (string, error) {
	videoID, err := resolveDouyinVideoID(ctx, rawURL)
	if err != nil {
		return "", err
	}

	shareURL := "https://www.iesdouyin.com/share/video/" + videoID + "/"
	document, err := fetchDouyinSharePage(ctx, shareURL)
	if err != nil {
		return "", fmt.Errorf("load Douyin share page: %w", err)
	}

	candidates, parseErr := douyinRouterDataCandidates(document)
	// DOM/raw-URL extraction is intentionally only a fallback. Structured
	// _ROUTER_DATA is less likely to accidentally select a cover/image URL.
	for _, candidate := range douyinMediaCandidates(document) {
		candidates = appendUniqueDouyinURL(candidates, candidate)
	}
	if len(candidates) == 0 {
		if parseErr != nil {
			return "", fmt.Errorf("Douyin _ROUTER_DATA parse failed: %w", parseErr)
		}
		return "", fmt.Errorf("Douyin share page contained no playable video URL")
	}

	cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE"))
	var failures []string
	maxCandidates := envPositiveInt("DOUYIN_MEDIA_CANDIDATES", 12)
	if maxCandidates > len(candidates) {
		maxCandidates = len(candidates)
	}
	for index, mediaURL := range candidates[:maxCandidates] {
		output := filepath.Join(outputDir, fmt.Sprintf("douyin-%s-%02d.mp4", videoID, index+1))
		if err := downloadDouyinMedia(ctx, mediaURL, shareURL, output, cookie); err != nil {
			failures = append(failures, fmt.Sprintf("candidate %d: %v", index+1, err))
			continue
		}
		if !hasAudioStream(output) {
			_ = os.Remove(output)
			failures = append(failures, fmt.Sprintf("candidate %d: downloaded media has no audio", index+1))
			continue
		}
		return output, nil
	}

	if len(failures) == 0 {
		return "", fmt.Errorf("Douyin download returned no usable media")
	}
	return "", fmt.Errorf("Douyin direct download failed: %s", strings.Join(failures, " | "))
}

func resolveDouyinVideoID(ctx context.Context, rawURL string) (string, error) {
	if id := extractDouyinDownloadVideoID(rawURL); id != "" {
		return id, nil
	}

	u, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil || u.Scheme == "" || u.Hostname() == "" || !douyinPageHostAllowed(u.Hostname()) {
		return "", fmt.Errorf("invalid Douyin URL")
	}

	client := &http.Client{
		Timeout: time.Duration(envPositiveInt("DOUYIN_RESOLVE_TIMEOUT_SEC", 18)) * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 10 {
				return fmt.Errorf("too many Douyin redirects")
			}
			if !douyinPageHostAllowed(req.URL.Hostname()) {
				return fmt.Errorf("Douyin redirect left an allowed host: %s", req.URL.Hostname())
			}
			return nil
		},
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return "", err
	}
	setDouyinPageHeaders(req)
	if cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE")); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}

	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("resolve Douyin URL: %w", err)
	}
	defer resp.Body.Close()
	if id := extractDouyinDownloadVideoID(resp.Request.URL.String()); id != "" {
		return id, nil
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return "", err
	}
	if id := extractDouyinDownloadVideoID(string(body)); id != "" {
		return id, nil
	}
	return "", fmt.Errorf("could not extract Douyin video id")
}

func extractDouyinDownloadVideoID(value string) string {
	if match := douyinDownloadVideoIDPattern.FindStringSubmatch(value); len(match) == 2 {
		return match[1]
	}
	if match := douyinModalIDPattern.FindStringSubmatch(value); len(match) == 2 {
		return match[1]
	}
	return ""
}

func douyinPageHostAllowed(hostname string) bool {
	host := strings.ToLower(strings.TrimPrefix(strings.TrimSpace(hostname), "www."))
	for _, suffix := range []string{"douyin.com", "iesdouyin.com", "amemv.com"} {
		if host == suffix || strings.HasSuffix(host, "."+suffix) {
			return true
		}
	}
	return false
}

func fetchDouyinSharePage(ctx context.Context, shareURL string) (string, error) {
	requestCtx, cancel := context.WithTimeout(ctx, time.Duration(envPositiveInt("DOUYIN_PAGE_TIMEOUT_SEC", 25))*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, shareURL, nil)
	if err != nil {
		return "", err
	}
	setDouyinPageHeaders(req)
	if cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE")); cookie != "" {
		req.Header.Set("Cookie", cookie)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 400 {
		return "", fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return "", err
	}
	return string(body), nil
}

func setDouyinPageHeaders(req *http.Request) {
	req.Header.Set("User-Agent", douyinMobileUserAgent())
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.6")
	req.Header.Set("Referer", "https://www.douyin.com/")
}

func douyinRouterDataCandidates(document string) ([]string, error) {
	match := douyinRouterDataPattern.FindStringIndex(document)
	if match == nil {
		return nil, fmt.Errorf("window._ROUTER_DATA was not found")
	}
	start := match[1]
	for start < len(document) && document[start] != '{' {
		start++
	}
	if start >= len(document) {
		return nil, fmt.Errorf("_ROUTER_DATA JSON start was not found")
	}
	jsonText := extractDouyinJSONObject(document, start)
	if jsonText == "" {
		return nil, fmt.Errorf("_ROUTER_DATA JSON object was incomplete")
	}

	var payload any
	decoder := json.NewDecoder(strings.NewReader(jsonText))
	decoder.UseNumber()
	if err := decoder.Decode(&payload); err != nil {
		return nil, fmt.Errorf("decode _ROUTER_DATA: %w", err)
	}

	videoInfoRes := findDouyinVideoInfoRes(payload)
	if videoInfoRes == nil {
		return nil, fmt.Errorf("videoInfoRes was not found in _ROUTER_DATA")
	}
	items := douyinSlice(videoInfoRes["item_list"])
	if len(items) == 0 {
		if filters := douyinSlice(videoInfoRes["filter_list"]); len(filters) > 0 {
			if filter := douyinMap(filters[0]); filter != nil {
				message := firstDouyinString(filter["detail_msg"], filter["notice"])
				if message != "" {
					return nil, fmt.Errorf("Douyin video unavailable: %s", message)
				}
			}
		}
		return nil, fmt.Errorf("Douyin video item_list was empty")
	}

	item := douyinMap(items[0])
	if item == nil {
		return nil, fmt.Errorf("Douyin video item had an unexpected shape")
	}
	video := douyinMap(item["video"])
	if video == nil {
		return nil, fmt.Errorf("Douyin item contained no video object")
	}

	var ranked []douyinScoredURL
	for _, raw := range douyinSlice(video["bit_rate"]) {
		bitrate := douyinMap(raw)
		if bitrate == nil {
			continue
		}
		score := douyinInt64(bitrate["bit_rate"])
		for _, key := range []string{"play_addr", "play_addr_h264", "play_addr_265"} {
			for _, mediaURL := range douyinURLList(douyinMap(bitrate[key])) {
				ranked = append(ranked, douyinScoredURL{URL: mediaURL, Bitrate: score})
			}
		}
	}
	sort.SliceStable(ranked, func(i, j int) bool { return ranked[i].Bitrate > ranked[j].Bitrate })

	candidates := make([]string, 0, len(ranked)+10)
	for _, candidate := range ranked {
		candidates = appendUniqueDouyinURL(candidates, candidate.URL)
	}

	var videoURI string
	for _, key := range []string{"play_addr", "play_addr_h264", "play_addr_265"} {
		playAddr := douyinMap(video[key])
		for _, mediaURL := range douyinURLList(playAddr) {
			candidates = appendUniqueDouyinURL(candidates, mediaURL)
		}
		if videoURI == "" && playAddr != nil {
			videoURI = strings.TrimSpace(douyinString(playAddr["uri"]))
		}
	}

	if videoURI != "" {
		ratios := []string{"1080p", "720p", "540p", "default"}
		if envDownloadBool("DOUYIN_PREFER_ORIGINAL", false) {
			ratios = []string{"default", "1080p", "720p", "540p"}
		}
		for _, ratio := range ratios {
			playURL := "https://aweme.snssdk.com/aweme/v1/play/?video_id=" + url.QueryEscape(videoURI) + "&ratio=" + url.QueryEscape(ratio) + "&line=0"
			candidates = appendUniqueDouyinURL(candidates, playURL)
		}
	}

	if len(candidates) == 0 {
		return nil, fmt.Errorf("_ROUTER_DATA contained no playable video URL")
	}
	return candidates, nil
}

func findDouyinVideoInfoRes(value any) map[string]any {
	switch current := value.(type) {
	case map[string]any:
		if result := douyinMap(current["videoInfoRes"]); result != nil {
			return result
		}
		for _, child := range current {
			if result := findDouyinVideoInfoRes(child); result != nil {
				return result
			}
		}
	case []any:
		for _, child := range current {
			if result := findDouyinVideoInfoRes(child); result != nil {
				return result
			}
		}
	}
	return nil
}

func extractDouyinJSONObject(document string, start int) string {
	depth := 0
	inString := false
	escaped := false
	for i := start; i < len(document); i++ {
		char := document[i]
		if escaped {
			escaped = false
			continue
		}
		if inString && char == '\\' {
			escaped = true
			continue
		}
		if char == '"' {
			inString = !inString
			continue
		}
		if inString {
			continue
		}
		switch char {
		case '{':
			depth++
		case '}':
			depth--
			if depth == 0 {
				return document[start : i+1]
			}
		}
	}
	return ""
}

func douyinURLList(container map[string]any) []string {
	if container == nil {
		return nil
	}
	values := douyinSlice(container["url_list"])
	out := make([]string, 0, len(values))
	for _, value := range values {
		if candidate := strings.TrimSpace(douyinString(value)); candidate != "" {
			out = append(out, candidate)
		}
	}
	return out
}

func appendUniqueDouyinURL(values []string, candidate string) []string {
	candidate = normalizeDouyinMediaURL(candidate)
	if candidate == "" {
		return values
	}
	for _, existing := range values {
		if existing == candidate {
			return values
		}
	}
	return append(values, candidate)
}

func normalizeDouyinMediaURL(candidate string) string {
	candidate = strings.TrimSpace(html.UnescapeString(candidate))
	candidate = strings.NewReplacer(
		`\u002F`, `/`,
		`\u002f`, `/`,
		`\u0026`, `&`,
		`\u003D`, `=`,
		`\u003d`, `=`,
		`\/`, `/`,
	).Replace(candidate)
	candidate = strings.TrimRight(candidate, `,;)]}`)
	if strings.HasPrefix(candidate, "//") {
		candidate = "https:" + candidate
	}
	candidate = strings.ReplaceAll(candidate, "playwm", "play")
	if strings.HasPrefix(candidate, "http://") {
		candidate = "https://" + strings.TrimPrefix(candidate, "http://")
	}
	parsed, err := url.Parse(candidate)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" {
		return ""
	}
	return candidate
}

// Raw media URLs embedded in HTML are a last-resort fallback for page variants
// where _ROUTER_DATA changes shape but the page still exposes a currentSrc/src.
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

	results := make([]string, 0, 8)
	appendCandidate := func(candidate string) {
		candidate = normalizeDouyinMediaURL(candidate)
		if candidate == "" {
			return
		}
		lower := strings.ToLower(candidate)
		if !strings.Contains(lower, ".mp4") &&
			!strings.Contains(lower, "douyinvod") &&
			!strings.Contains(lower, "mime_type=video") &&
			!strings.Contains(lower, "/play/") {
			return
		}
		results = appendUniqueDouyinURL(results, candidate)
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
	req.Header.Set("User-Agent", douyinMobileUserAgent())
	req.Header.Set("Accept", "video/*,application/octet-stream;q=0.9,*/*;q=0.5")
	req.Header.Set("Referer", referer)
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

func douyinMobileUserAgent() string {
	if value := strings.TrimSpace(os.Getenv("DOUYIN_MOBILE_USER_AGENT")); value != "" {
		return value
	}
	return "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
}

func douyinMap(value any) map[string]any {
	result, _ := value.(map[string]any)
	return result
}

func douyinSlice(value any) []any {
	result, _ := value.([]any)
	return result
}

func douyinString(value any) string {
	switch v := value.(type) {
	case string:
		return v
	case json.Number:
		return v.String()
	case float64:
		return strconv.FormatFloat(v, 'f', -1, 64)
	default:
		return ""
	}
}

func firstDouyinString(values ...any) string {
	for _, value := range values {
		if result := strings.TrimSpace(douyinString(value)); result != "" {
			return result
		}
	}
	return ""
}

func douyinInt64(value any) int64 {
	switch v := value.(type) {
	case json.Number:
		result, _ := v.Int64()
		return result
	case float64:
		return int64(v)
	case int64:
		return v
	case int:
		return int64(v)
	case string:
		result, _ := strconv.ParseInt(v, 10, 64)
		return result
	default:
		return 0
	}
}

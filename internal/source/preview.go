package source

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type previewCacheEntry struct {
	video   model.Video
	err     string
	expires time.Time
}

var (
	previewCacheMu sync.Mutex
	previewCache   = map[string]previewCacheEntry{}
	previewSemOnce sync.Once
	previewSem     chan struct{}
	metaTagPattern = regexp.MustCompile(`(?is)<meta\s+[^>]*>`)
	metaAttrPattern = regexp.MustCompile(`(?is)([a-zA-Z_:.-]+)\s*=\s*["']([^"']*)["']`)
	xhsVideoTypePattern = regexp.MustCompile(`(?is)"type"\s*:\s*"video"|"noteType"\s*:\s*"video"|"originVideoKey"\s*:|"media"\s*:\s*\{\s*"stream"`)
	xhsImageTypePattern = regexp.MustCompile(`(?is)"type"\s*:\s*"normal"|"noteType"\s*:\s*"normal"`)
)

// EnrichPreview resolves thumbnail and lightweight metadata for a discovered URL without downloading the video.
// It only accepts known public video hosts so the HTTP endpoint using this function cannot become a generic SSRF proxy.
func EnrichPreview(ctx context.Context, video model.Video) (model.Video, error) {
	if strings.TrimSpace(video.URL) == "" {
		return video, fmt.Errorf("preview URL is required")
	}
	u, err := url.Parse(video.URL)
	if err != nil || u.Scheme == "" || u.Hostname() == "" {
		return video, fmt.Errorf("invalid preview URL")
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return video, fmt.Errorf("unsupported preview URL scheme")
	}
	if !previewHostAllowed(video.Platform, u.Hostname()) {
		return video, fmt.Errorf("preview host is not allowed for platform %q", video.Platform)
	}

	key := strings.ToLower(strings.TrimSpace(video.Platform)) + "|" + u.String()
	if cached, ok := getPreviewCache(key); ok {
		return mergePreview(video, cached.video), errorFromString(cached.err)
	}

	sem := previewSemaphore()
	select {
	case sem <- struct{}{}:
		defer func() { <-sem }()
	case <-ctx.Done():
		return video, ctx.Err()
	}

	// A normal page fetch is much cheaper than starting yt-dlp. Many platforms expose og:image even
	// when their full video extractor requires JavaScript or login, so try it first.
	enriched := video
	var errs []string
	if page, pageErr := previewOpenGraph(ctx, enriched); pageErr == nil {
		enriched = page
	} else {
		errs = append(errs, "page="+pageErr.Error())
	}

	deep := envPreviewBool("PREVIEW_DEEP_METADATA", false)
	platform := strings.ToLower(strings.TrimSpace(enriched.Platform))
	// Xiaohongshu public search mixes image notes and video notes. If the cheap page
	// probe could not identify the type, ask yt-dlp for metadata even when og:image exists.
	needYTDLP := strings.TrimSpace(enriched.Thumbnail) == "" || deep || (platform == "xiaohongshu" && strings.TrimSpace(enriched.MediaType) == "")
	if needYTDLP {
		if metadata, metadataErr := previewYTDLP(ctx, enriched); metadataErr == nil {
			enriched = metadata
		} else {
			errs = append(errs, "yt-dlp="+metadataErr.Error())
		}
	}

	if strings.TrimSpace(enriched.Thumbnail) != "" {
		putPreviewCache(key, enriched, "", 30*time.Minute)
		return enriched, nil
	}
	message := "preview metadata unavailable"
	if len(errs) > 0 {
		message += ": " + strings.Join(errs, "; ")
	}
	putPreviewCache(key, enriched, message, 3*time.Minute)
	return enriched, fmt.Errorf("%s", message)
}

func previewSemaphore() chan struct{} {
	previewSemOnce.Do(func() {
		concurrency := 4
		if raw := strings.TrimSpace(os.Getenv("PREVIEW_CONCURRENCY")); raw != "" {
			if n, err := strconv.Atoi(raw); err == nil && n > 0 && n <= 12 {
				concurrency = n
			}
		}
		previewSem = make(chan struct{}, concurrency)
	})
	return previewSem
}

func getPreviewCache(key string) (previewCacheEntry, bool) {
	previewCacheMu.Lock()
	defer previewCacheMu.Unlock()
	entry, ok := previewCache[key]
	if !ok {
		return previewCacheEntry{}, false
	}
	if time.Now().After(entry.expires) {
		delete(previewCache, key)
		return previewCacheEntry{}, false
	}
	return entry, true
}

func putPreviewCache(key string, video model.Video, err string, ttl time.Duration) {
	previewCacheMu.Lock()
	previewCache[key] = previewCacheEntry{video: video, err: err, expires: time.Now().Add(ttl)}
	previewCacheMu.Unlock()
}

func errorFromString(value string) error {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	return fmt.Errorf("%s", value)
}

func previewHostAllowed(platform, hostname string) bool {
	host := strings.ToLower(strings.TrimPrefix(hostname, "www."))
	platform = strings.ToLower(strings.TrimSpace(platform))
	allowed := map[string][]string{
		"bilibili":    {"bilibili.com", "b23.tv"},
		"douyin":      {"douyin.com", "iesdouyin.com"},
		"kuaishou":    {"kuaishou.com", "gifshow.com"},
		"xiaohongshu": {"xiaohongshu.com", "xhslink.com"},
		"weibo":       {"weibo.com", "weibo.cn"},
		"xigua":       {"ixigua.com"},
		"haokan":      {"haokan.baidu.com"},
		"toutiao":     {"toutiao.com"},
		"acfun":       {"acfun.cn"},
		"meipai":      {"meipai.com"},
		"weishi":      {"weishi.qq.com"},
	}
	for _, suffix := range allowed[platform] {
		if host == suffix || strings.HasSuffix(host, "."+suffix) {
			return true
		}
	}
	return false
}

func previewOpenGraph(ctx context.Context, video model.Video) (model.Video, error) {
	timeout := envPreviewSeconds("PREVIEW_PAGE_TIMEOUT_SEC", 5)
	requestCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, video.URL, nil)
	if err != nil {
		return video, err
	}
	req.Header.Set("User-Agent", previewUserAgent())
	req.Header.Set("Accept", "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return video, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 400 {
		return video, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 3<<20))
	if err != nil {
		return video, err
	}
	document := string(body)
	meta := parsePreviewMeta(document, resp.Request.URL)
	if title := strings.TrimSpace(meta["title"]); title != "" && !genericPreviewTitle(title) {
		video.Title = title
	}
	if image := strings.TrimSpace(meta["image"]); image != "" {
		video.Thumbnail = image
	}
	if mediaType := strings.TrimSpace(meta["mediaType"]); mediaType != "" {
		video.MediaType = mediaType
	}
	if strings.EqualFold(strings.TrimSpace(video.Platform), "xiaohongshu") {
		if mediaType := detectXiaohongshuMediaType(document, video.URL); mediaType != "" {
			video.MediaType = mediaType
		}
	}
	if video.Thumbnail == "" {
		return video, fmt.Errorf("page contains no preview image")
	}
	return video, nil
}

func parsePreviewMeta(document string, baseURL *url.URL) map[string]string {
	result := map[string]string{}
	for _, tag := range metaTagPattern.FindAllString(document, 120) {
		attrs := map[string]string{}
		for _, match := range metaAttrPattern.FindAllStringSubmatch(tag, -1) {
			if len(match) == 3 {
				attrs[strings.ToLower(match[1])] = html.UnescapeString(strings.TrimSpace(match[2]))
			}
		}
		key := strings.ToLower(strings.TrimSpace(attrs["property"]))
		if key == "" {
			key = strings.ToLower(strings.TrimSpace(attrs["name"]))
		}
		content := strings.TrimSpace(attrs["content"])
		switch key {
		case "og:image", "twitter:image", "twitter:image:src":
			if result["image"] == "" && content != "" {
				if ref, err := url.Parse(content); err == nil {
					result["image"] = baseURL.ResolveReference(ref).String()
				}
			}
		case "og:title", "twitter:title":
			if result["title"] == "" {
				result["title"] = content
			}
		case "og:video", "og:video:url", "og:video:secure_url", "twitter:player":
			if content != "" {
				result["mediaType"] = "video"
			}
		case "og:type":
			if strings.Contains(strings.ToLower(content), "video") {
				result["mediaType"] = "video"
			}
		}
	}
	return result
}

func detectXiaohongshuMediaType(document, rawURL string) string {
	// Detail pages carry noteDetailMap keyed by the note id. Limit the heuristic
	// to the chunk around that id so unrelated recommendation cards do not decide
	// the type of the selected note.
	section := document
	if u, err := url.Parse(rawURL); err == nil {
		parts := strings.Split(strings.Trim(u.Path, "/"), "/")
		if len(parts) > 0 {
			id := parts[len(parts)-1]
			if len(id) >= 16 {
				if idx := strings.Index(document, id); idx >= 0 {
					start := idx - 4096
					if start < 0 { start = 0 }
					end := idx + 350000
					if end > len(document) { end = len(document) }
					section = document[start:end]
				}
			}
		}
	}
	if xhsVideoTypePattern.MatchString(section) {
		return "video"
	}
	if xhsImageTypePattern.MatchString(section) {
		return "image"
	}
	return ""
}

func genericPreviewTitle(title string) bool {
	lower := strings.ToLower(strings.TrimSpace(title))
	for _, marker := range []string{"登录", "login", "首页", "home -", "安全验证", "验证码", "你的生活兴趣社区"} {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

type ytPreview struct {
	ID           string  `json:"id"`
	Title        string  `json:"title"`
	Uploader     string  `json:"uploader"`
	Channel      string  `json:"channel"`
	Thumbnail    string  `json:"thumbnail"`
	Duration     float64 `json:"duration"`
	ViewCount    int64   `json:"view_count"`
	LikeCount    int64   `json:"like_count"`
	CommentCount int64   `json:"comment_count"`
	Timestamp    int64   `json:"timestamp"`
	UploadDate   string  `json:"upload_date"`
	Formats      []json.RawMessage `json:"formats"`
	Thumbnails   []struct {
		URL string `json:"url"`
	} `json:"thumbnails"`
}

func previewYTDLP(ctx context.Context, video model.Video) (model.Video, error) {
	bin, err := exec.LookPath("yt-dlp")
	if err != nil {
		return video, fmt.Errorf("yt-dlp not installed: %w", err)
	}
	timeout := envPreviewSeconds("PREVIEW_YTDLP_TIMEOUT_SEC", 12)
	requestCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(requestCtx, bin,
		"--ignore-config",
		"--skip-download",
		"--no-playlist",
		"--no-warnings",
		"--ignore-no-formats-error",
		"--socket-timeout", "8",
		"--retries", "1",
		"--extractor-retries", "1",
		"--user-agent", previewUserAgent(),
		"--dump-single-json",
		video.URL,
	)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if requestCtx.Err() != nil {
			return video, fmt.Errorf("metadata timed out after %s", timeout)
		}
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		if len(message) > 700 {
			message = message[:700]
		}
		return video, fmt.Errorf("metadata failed: %s", message)
	}
	var payload ytPreview
	if err := json.Unmarshal(stdout.Bytes(), &payload); err != nil {
		return video, fmt.Errorf("decode metadata: %w", err)
	}
	if payload.ID != "" {
		video.ID = payload.ID
	}
	if title := strings.TrimSpace(payload.Title); title != "" && !genericPreviewTitle(title) {
		video.Title = title
	}
	if uploader := strings.TrimSpace(payload.Uploader); uploader != "" {
		video.Author = uploader
	} else if channel := strings.TrimSpace(payload.Channel); channel != "" {
		video.Author = channel
	}
	thumbnail := strings.TrimSpace(payload.Thumbnail)
	if thumbnail == "" {
		for i := len(payload.Thumbnails) - 1; i >= 0; i-- {
			if candidate := strings.TrimSpace(payload.Thumbnails[i].URL); candidate != "" {
				thumbnail = candidate
				break
			}
		}
	}
	if thumbnail != "" {
		video.Thumbnail = thumbnail
	}
	if len(payload.Formats) > 0 {
		video.MediaType = "video"
	}
	if payload.Duration > 0 {
		video.DurationSec = int64(payload.Duration + 0.5)
	}
	if payload.ViewCount > 0 {
		video.Views = payload.ViewCount
	}
	if payload.LikeCount > 0 {
		video.Likes = payload.LikeCount
	}
	if payload.CommentCount > 0 {
		video.Comments = payload.CommentCount
	}
	if payload.Timestamp > 0 {
		t := time.Unix(payload.Timestamp, 0).UTC()
		video.PublishedAt = &t
	} else if len(payload.UploadDate) == 8 {
		if t, err := time.Parse("20060102", payload.UploadDate); err == nil {
			t = t.UTC()
			video.PublishedAt = &t
		}
	}
	if video.Thumbnail == "" {
		return video, fmt.Errorf("yt-dlp returned no thumbnail")
	}
	return video, nil
}

func mergePreview(original, enriched model.Video) model.Video {
	if enriched.ID != "" { original.ID = enriched.ID }
	if enriched.Title != "" { original.Title = enriched.Title }
	if enriched.Author != "" { original.Author = enriched.Author }
	if enriched.Thumbnail != "" { original.Thumbnail = enriched.Thumbnail }
	if enriched.MediaType != "" { original.MediaType = enriched.MediaType }
	if enriched.DurationSec > 0 { original.DurationSec = enriched.DurationSec }
	if enriched.Views > 0 { original.Views = enriched.Views }
	if enriched.Likes > 0 { original.Likes = enriched.Likes }
	if enriched.Comments > 0 { original.Comments = enriched.Comments }
	if enriched.Shares > 0 { original.Shares = enriched.Shares }
	if enriched.PublishedAt != nil { original.PublishedAt = enriched.PublishedAt }
	return original
}

func previewUserAgent() string {
	if value := strings.TrimSpace(os.Getenv("PREVIEW_USER_AGENT")); value != "" {
		return value
	}
	return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
}

func envPreviewSeconds(name string, fallback int) time.Duration {
	if raw := strings.TrimSpace(os.Getenv(name)); raw != "" {
		if seconds, err := strconv.Atoi(raw); err == nil && seconds > 0 && seconds <= 120 {
			return time.Duration(seconds) * time.Second
		}
	}
	return time.Duration(fallback) * time.Second
}

func envPreviewBool(name string, fallback bool) bool {
	raw := strings.ToLower(strings.TrimSpace(os.Getenv(name)))
	if raw == "" {
		return fallback
	}
	return raw != "0" && raw != "false" && raw != "no" && raw != "off"
}

package source

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/xml"
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
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type DouyinProvider struct {
	mode   string
	python string
	bridge string
	cdpURL string
}

func NewDouyinProvider() *DouyinProvider {
	mode := strings.ToLower(strings.TrimSpace(os.Getenv("DOUYIN_MODE")))
	switch mode {
	case "", "auto", "authenticated":
		mode = "browser"
	case "guest":
		mode = "public"
	}
	python := strings.TrimSpace(os.Getenv("DOUYIN_PYTHON"))
	if python == "" {
		python = "python3"
	}
	bridge := strings.TrimSpace(os.Getenv("DOUYIN_BROWSER_BRIDGE"))
	if bridge == "" {
		bridge = "/app/scripts/douyin_browser_bridge.py"
	}
	cdpURL := strings.TrimRight(strings.TrimSpace(os.Getenv("DOUYIN_CDP_URL")), "/")
	if cdpURL == "" {
		cdpURL = "http://host.docker.internal:9222"
	}
	return &DouyinProvider{
		mode:   mode,
		python: python,
		bridge: bridge,
		cdpURL: cdpURL,
	}
}

func (p *DouyinProvider) Name() string { return "douyin" }

func (p *DouyinProvider) Available() error {
	switch p.mode {
	case "public", "hybrid":
		return nil
	case "browser":
		return p.browserAvailable()
	default:
		return fmt.Errorf("invalid DOUYIN_MODE %q; use browser, hybrid, or public", p.mode)
	}
}

func (p *DouyinProvider) browserAvailable() error {
	if _, err := executable(p.python); err != nil {
		return err
	}
	if info, err := os.Stat(p.bridge); err != nil || info.IsDir() {
		if err == nil {
			err = fmt.Errorf("path is a directory")
		}
		return fmt.Errorf("Douyin browser bridge unavailable at %s: %w", p.bridge, err)
	}

	endpoint := p.cdpURL + "/json/version"
	client := &http.Client{Timeout: envSeconds("DOUYIN_BROWSER_CONNECT_TIMEOUT_SEC", 3)}
	req, err := http.NewRequest(http.MethodGet, endpoint, nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf(
			"Douyin browser CDP is not reachable at %s; run scripts/start_douyin_browser.ps1 on Windows and keep that browser open: %w",
			p.cdpURL,
			err,
		)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Douyin browser CDP returned HTTP %d at %s", resp.StatusCode, endpoint)
	}
	return nil
}

func (p *DouyinProvider) Search(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	keyword = strings.TrimSpace(keyword)
	if keyword == "" {
		return nil, fmt.Errorf("douyin keyword is required")
	}
	if limit <= 0 {
		limit = 10
	}

	switch p.mode {
	case "browser":
		return p.searchBrowser(ctx, keyword, limit)
	case "hybrid":
		results, browserErr := p.searchBrowser(ctx, keyword, limit)
		if browserErr == nil && len(results) > 0 {
			return results, nil
		}
		publicResults, publicErr := p.searchPublicIndex(ctx, keyword, limit)
		if publicErr == nil && len(publicResults) > 0 {
			return publicResults, nil
		}
		return nil, fmt.Errorf("douyin hybrid discovery failed: browser=%v; public-index=%v", browserErr, publicErr)
	case "public":
		return p.searchPublicIndex(ctx, keyword, limit)
	default:
		return nil, fmt.Errorf("invalid DOUYIN_MODE %q; use browser, hybrid, or public", p.mode)
	}
}

func (p *DouyinProvider) searchBrowser(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	python, err := executable(p.python)
	if err != nil {
		return nil, err
	}
	if info, err := os.Stat(p.bridge); err != nil || info.IsDir() {
		if err == nil {
			err = fmt.Errorf("path is a directory")
		}
		return nil, fmt.Errorf("Douyin browser bridge unavailable at %s: %w", p.bridge, err)
	}

	timeout := envSeconds("DOUYIN_BROWSER_SEARCH_TIMEOUT_SEC", 75)
	attemptCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(
		attemptCtx,
		python,
		p.bridge,
		"search",
		"--keyword", keyword,
		"--limit", strconv.Itoa(limit),
	)
	cmd.Env = os.Environ()
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if attemptCtx.Err() != nil {
			return nil, fmt.Errorf("Douyin browser search timed out after %s", timeout)
		}
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return nil, fmt.Errorf("Douyin browser search failed: %s", message)
	}
	results, err := parseDouyinMetadata(stdout.Bytes(), keyword)
	if err != nil {
		return nil, err
	}
	if len(results) == 0 {
		return nil, fmt.Errorf("Douyin browser search returned no video candidates for %q", keyword)
	}
	return results, nil
}

func parseDouyinMetadata(data []byte, keyword string) ([]model.Video, error) {
	var items []map[string]any
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(&items); err != nil {
		return nil, fmt.Errorf("parse Douyin browser metadata: %w", err)
	}

	results := make([]model.Video, 0, len(items))
	for _, item := range items {
		id := asString(item["id"])
		if id == "" {
			continue
		}
		duration := asInt64(item["duration"])
		if duration > 10000 {
			duration /= 1000
		}
		searchSource := strings.TrimSpace(asString(item["search_source"]))
		if searchSource == "" {
			searchSource = keyword
		}
		v := model.Video{
			ID:           id,
			Platform:     "douyin",
			Title:        asString(item["desc"]),
			Author:       asString(item["author_nickname"]),
			URL:          "https://www.douyin.com/video/" + id,
			Thumbnail:    asString(item["cover"]),
			MediaType:    "video",
			DurationSec:  duration,
			Views:        asInt64(item["play_count"]),
			Likes:        asInt64(item["digg_count"]),
			Comments:     asInt64(item["comment_count"]),
			Shares:       asInt64(item["share_count"]),
			DownloadURL:  asString(item["download_addr"]),
			SearchSource: searchSource,
		}
		if ts := asInt64(item["time"]); ts > 0 {
			t := time.Unix(ts, 0).UTC()
			v.PublishedAt = &t
		}
		results = append(results, v)
	}
	return results, nil
}

type guestRSS struct {
	Channel struct {
		Items []struct {
			Title       string `xml:"title"`
			Link        string `xml:"link"`
			Description string `xml:"description"`
		} `xml:"item"`
	} `xml:"channel"`
}

var douyinVideoPath = regexp.MustCompile(`(?i)/video/([0-9]{8,})`)

func (p *DouyinProvider) searchPublicIndex(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	if limit > 50 {
		limit = 50
	}
	endpoint := strings.TrimSpace(os.Getenv("DOUYIN_GUEST_SEARCH_URL"))
	if endpoint == "" {
		values := url.Values{}
		values.Set("format", "rss")
		values.Set("count", strconv.Itoa(limit))
		values.Set("q", "site:douyin.com/video "+keyword)
		endpoint = "https://www.bing.com/search?" + values.Encode()
	}

	requestCtx, cancel := context.WithTimeout(ctx, envSeconds("DOUYIN_GUEST_TIMEOUT_SEC", 20))
	defer cancel()
	req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	userAgent := strings.TrimSpace(os.Getenv("DOUYIN_GUEST_USER_AGENT"))
	if userAgent == "" {
		userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.5")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("public search index returned HTTP %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, err
	}
	return parseGuestRSS(data, keyword, limit)
}

func parseGuestRSS(data []byte, keyword string, limit int) ([]model.Video, error) {
	var feed guestRSS
	if err := xml.Unmarshal(data, &feed); err != nil {
		return nil, fmt.Errorf("parse public search RSS: %w", err)
	}

	seen := make(map[string]struct{})
	results := make([]model.Video, 0, len(feed.Channel.Items))
	for _, item := range feed.Channel.Items {
		link := strings.TrimSpace(html.UnescapeString(item.Link))
		id := extractDouyinVideoID(link)
		if id == "" {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}

		title := cleanGuestTitle(item.Title)
		if title == "" {
			title = cleanGuestTitle(item.Description)
		}
		if title == "" {
			title = "Douyin video " + id
		}
		results = append(results, model.Video{
			ID:           id,
			Platform:     "douyin",
			Title:        title,
			URL:          "https://www.douyin.com/video/" + id,
			MediaType:    "video",
			SearchSource: keyword,
		})
		if len(results) >= limit {
			break
		}
	}
	return results, nil
}

func extractDouyinVideoID(rawURL string) string {
	match := douyinVideoPath.FindStringSubmatch(rawURL)
	if len(match) != 2 {
		return ""
	}
	return match[1]
}

func cleanGuestTitle(value string) string {
	value = strings.TrimSpace(html.UnescapeString(value))
	for _, suffix := range []string{" - 抖音", "_抖音", " | 抖音", "- 抖音"} {
		value = strings.TrimSpace(strings.TrimSuffix(value, suffix))
	}
	return value
}

func envSeconds(name string, fallback int) time.Duration {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return time.Duration(fallback) * time.Second
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return time.Duration(fallback) * time.Second
	}
	return time.Duration(parsed) * time.Second
}

func asString(value any) string {
	switch v := value.(type) {
	case string:
		return v
	case json.Number:
		return v.String()
	case float64:
		return strconv.FormatInt(int64(v), 10)
	default:
		return ""
	}
}

func asInt64(value any) int64 {
	switch v := value.(type) {
	case float64:
		return int64(v)
	case int64:
		return v
	case int:
		return int64(v)
	case json.Number:
		n, _ := v.Int64()
		return n
	case string:
		n, _ := strconv.ParseInt(v, 10, 64)
		return n
	default:
		return 0
	}
}

package source

import (
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
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type DouyinProvider struct {
	binary string
	mode   string
}

func NewDouyinProvider() *DouyinProvider {
	binary := strings.TrimSpace(os.Getenv("DOUYIN_BIN"))
	if binary == "" {
		binary = "douyin"
	}
	mode := strings.ToLower(strings.TrimSpace(os.Getenv("DOUYIN_MODE")))
	if mode == "" {
		mode = "auto"
	}
	return &DouyinProvider{binary: binary, mode: mode}
}

func (p *DouyinProvider) Name() string { return "douyin" }

func (p *DouyinProvider) Available() error {
	switch p.mode {
	case "auto", "guest":
		// Guest discovery can fall back to a public search index, so douyin-cli
		// itself is not a hard requirement for search availability.
		return nil
	case "authenticated":
		if strings.TrimSpace(os.Getenv("DOUYIN_COOKIE")) == "" {
			return fmt.Errorf("DOUYIN_MODE=authenticated requires DOUYIN_COOKIE")
		}
		_, err := executable(p.binary)
		return err
	default:
		return fmt.Errorf("invalid DOUYIN_MODE %q; use auto, authenticated, or guest", p.mode)
	}
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
	case "authenticated":
		cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE"))
		if cookie == "" {
			return nil, fmt.Errorf("DOUYIN_MODE=authenticated requires DOUYIN_COOKIE")
		}
		return p.searchCLI(ctx, keyword, limit, cookie, envSeconds("DOUYIN_AUTH_TIMEOUT_SEC", 60))
	case "guest":
		return p.searchGuest(ctx, keyword, limit)
	case "auto":
		cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE"))
		var authErr error
		if cookie != "" {
			results, err := p.searchCLI(ctx, keyword, limit, cookie, envSeconds("DOUYIN_AUTH_TIMEOUT_SEC", 40))
			if err == nil && len(results) > 0 {
				return results, nil
			}
			authErr = err
		}

		results, guestErr := p.searchGuest(ctx, keyword, limit)
		if guestErr == nil && len(results) > 0 {
			return results, nil
		}
		if authErr != nil {
			return nil, fmt.Errorf("douyin auto discovery failed: authenticated=%v; guest=%v", authErr, guestErr)
		}
		return nil, guestErr
	default:
		return nil, fmt.Errorf("invalid DOUYIN_MODE %q; use auto, authenticated, or guest", p.mode)
	}
}

func (p *DouyinProvider) searchGuest(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	var cliErr error
	if _, err := executable(p.binary); err == nil {
		results, err := p.searchCLI(ctx, keyword, limit, "", envSeconds("DOUYIN_GUEST_CLI_TIMEOUT_SEC", 18))
		if err == nil && len(results) > 0 {
			return results, nil
		}
		cliErr = err
	}

	results, webErr := p.searchPublicIndex(ctx, keyword, limit)
	if webErr == nil && len(results) > 0 {
		return results, nil
	}
	if cliErr != nil {
		return nil, fmt.Errorf("douyin guest discovery failed: cli=%v; public-index=%v", cliErr, webErr)
	}
	if webErr != nil {
		return nil, fmt.Errorf("douyin guest public discovery failed: %w", webErr)
	}
	return nil, fmt.Errorf("douyin guest discovery returned no public videos for %q", keyword)
}

func (p *DouyinProvider) searchCLI(ctx context.Context, keyword string, limit int, cookie string, timeout time.Duration) ([]model.Video, error) {
	bin, err := executable(p.binary)
	if err != nil {
		return nil, err
	}

	attemptCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	tmp, err := os.MkdirTemp("", "videoget-douyin-*")
	if err != nil {
		return nil, fmt.Errorf("create temp dir: %w", err)
	}
	defer os.RemoveAll(tmp)

	cmd := exec.CommandContext(attemptCtx, bin,
		"-u", keyword,
		"-t", "search",
		"-l", strconv.Itoa(limit),
		"--no-download",
		"-p", tmp,
	)
	cmd.Env = douyinCommandEnv(cookie)
	output, err := cmd.CombinedOutput()
	if err != nil {
		if attemptCtx.Err() != nil {
			return nil, fmt.Errorf("douyin search timed out after %s", timeout)
		}
		return nil, fmt.Errorf("douyin search failed: %s", strings.TrimSpace(string(output)))
	}

	files, _ := filepath.Glob(filepath.Join(tmp, "*.json"))
	if len(files) == 0 {
		return nil, fmt.Errorf("douyin-cli completed but no metadata JSON was produced")
	}
	sort.Strings(files)
	data, err := os.ReadFile(files[len(files)-1])
	if err != nil {
		return nil, err
	}
	return parseDouyinMetadata(data, keyword)
}

func parseDouyinMetadata(data []byte, keyword string) ([]model.Video, error) {
	var items []map[string]any
	if err := json.Unmarshal(data, &items); err != nil {
		return nil, fmt.Errorf("parse douyin metadata: %w", err)
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
		v := model.Video{
			ID:           id,
			Platform:     "douyin",
			Title:        asString(item["desc"]),
			Author:       asString(item["author_nickname"]),
			URL:          "https://www.douyin.com/video/" + id,
			Thumbnail:    asString(item["cover"]),
			DurationSec:  duration,
			Likes:        asInt64(item["digg_count"]),
			Comments:     asInt64(item["comment_count"]),
			Shares:       asInt64(item["share_count"]),
			DownloadURL:  asString(item["download_addr"]),
			SearchSource: keyword,
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

func douyinCommandEnv(cookie string) []string {
	env := make([]string, 0, len(os.Environ())+1)
	for _, item := range os.Environ() {
		if strings.HasPrefix(item, "DOUYIN_COOKIE=") {
			continue
		}
		env = append(env, item)
	}
	if strings.TrimSpace(cookie) != "" {
		env = append(env, "DOUYIN_COOKIE="+cookie)
	}
	return env
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

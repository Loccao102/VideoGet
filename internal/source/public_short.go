package source

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/xml"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type publicShortConfig struct {
	name      string
	query     string
	hosts     []string
	acceptURL func(*url.URL) bool
}

type PublicShortProvider struct {
	config publicShortConfig
}

func NewPublicShortProviders() []Provider {
	configs := []publicShortConfig{
		{
			name:  "kuaishou",
			query: "site:kuaishou.com/short-video",
			hosts: []string{"kuaishou.com"},
			acceptURL: func(u *url.URL) bool {
				return strings.Contains(strings.ToLower(u.Path), "/short-video/")
			},
		},
		{
			name:  "xiaohongshu",
			query: "site:xiaohongshu.com/explore",
			hosts: []string{"xiaohongshu.com"},
			acceptURL: func(u *url.URL) bool {
				path := strings.ToLower(u.Path)
				return strings.Contains(path, "/explore/") || strings.Contains(path, "/discovery/item/")
			},
		},
		{
			name:  "weibo",
			query: "site:weibo.com/tv/show",
			hosts: []string{"weibo.com"},
			acceptURL: func(u *url.URL) bool {
				path := strings.Trim(u.Path, "/")
				if strings.Contains(strings.ToLower(u.Path), "/tv/show/") {
					return true
				}
				parts := strings.Split(path, "/")
				return len(parts) == 2 && allDigits(parts[0]) && alphaNumeric(parts[1])
			},
		},
		{
			name:  "xigua",
			query: "site:ixigua.com 视频",
			hosts: []string{"ixigua.com"},
			acceptURL: func(u *url.URL) bool {
				path := strings.Trim(u.Path, "/")
				return len(path) >= 8 && allDigits(path)
			},
		},
		{
			name:  "haokan",
			query: "site:haokan.baidu.com/v",
			hosts: []string{"haokan.baidu.com"},
			acceptURL: func(u *url.URL) bool {
				return strings.TrimRight(strings.ToLower(u.Path), "/") == "/v" && strings.TrimSpace(u.Query().Get("vid")) != ""
			},
		},
		{
			name:  "toutiao",
			query: "site:toutiao.com/video",
			hosts: []string{"toutiao.com"},
			acceptURL: func(u *url.URL) bool {
				return strings.Contains(strings.ToLower(u.Path), "/video/")
			},
		},
		{
			name:  "acfun",
			query: "site:acfun.cn/v/ac",
			hosts: []string{"acfun.cn"},
			acceptURL: func(u *url.URL) bool {
				return strings.Contains(strings.ToLower(u.Path), "/v/ac")
			},
		},
		{
			name:  "meipai",
			query: "site:meipai.com/media",
			hosts: []string{"meipai.com"},
			acceptURL: func(u *url.URL) bool {
				return strings.Contains(strings.ToLower(u.Path), "/media/")
			},
		},
		{
			name:  "weishi",
			query: "site:weishi.qq.com 视频",
			hosts: []string{"weishi.qq.com"},
			acceptURL: func(u *url.URL) bool {
				path := strings.ToLower(u.Path)
				return strings.Contains(path, "/t/") || strings.Contains(path, "/video/")
			},
		},
	}

	providers := make([]Provider, 0, len(configs))
	for _, config := range configs {
		providers = append(providers, &PublicShortProvider{config: config})
	}
	return providers
}

func (p *PublicShortProvider) Name() string { return p.config.name }
func (p *PublicShortProvider) Available() error { return nil }

func (p *PublicShortProvider) Search(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	keyword = strings.TrimSpace(keyword)
	if keyword == "" {
		return nil, fmt.Errorf("%s keyword is required", p.config.name)
	}
	if limit <= 0 {
		limit = 10
	}
	if limit > 50 {
		limit = 50
	}

	query := strings.TrimSpace(p.config.query + " " + keyword)
	var combined []indexedSearchItem
	var errors []string

	bing, err := publicIndexBing(ctx, query, limit)
	if err != nil {
		errors = append(errors, "bing="+err.Error())
	} else {
		combined = append(combined, bing...)
	}

	if len(combined) < limit {
		duck, err := publicIndexDuckDuckGo(ctx, query, limit)
		if err != nil {
			errors = append(errors, "duckduckgo="+err.Error())
		} else {
			combined = append(combined, duck...)
		}
	}

	videos := p.normalize(combined, keyword, limit)
	if len(videos) > 0 {
		return videos, nil
	}
	// Zero indexed results is a valid state and should not flood the UI with red errors.
	// Only return an error if both public indexes themselves were unreachable/broken.
	if len(errors) >= 2 {
		return nil, fmt.Errorf("%s public discovery unavailable: %s", p.config.name, strings.Join(errors, "; "))
	}
	return []model.Video{}, nil
}

type indexedSearchItem struct {
	Title string
	URL   string
}

type publicRSS struct {
	Channel struct {
		Items []struct {
			Title       string `xml:"title"`
			Link        string `xml:"link"`
			Description string `xml:"description"`
		} `xml:"item"`
	} `xml:"channel"`
}

var (
	publicIndexOnce sync.Once
	publicIndexSem  chan struct{}
	anchorPattern   = regexp.MustCompile(`(?is)<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>`)
	tagPattern      = regexp.MustCompile(`(?is)<[^>]+>`)
)

func publicSemaphore() chan struct{} {
	publicIndexOnce.Do(func() {
		concurrency := 4
		if raw := strings.TrimSpace(os.Getenv("PUBLIC_INDEX_CONCURRENCY")); raw != "" {
			if parsed, err := strconv.Atoi(raw); err == nil && parsed > 0 && parsed <= 16 {
				concurrency = parsed
			}
		}
		publicIndexSem = make(chan struct{}, concurrency)
	})
	return publicIndexSem
}

func publicIndexBing(ctx context.Context, query string, limit int) ([]indexedSearchItem, error) {
	values := url.Values{}
	values.Set("format", "rss")
	values.Set("count", strconv.Itoa(limit))
	values.Set("q", query)
	endpoint := "https://www.bing.com/search?" + values.Encode()
	data, err := publicGET(ctx, endpoint, "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.5")
	if err != nil {
		return nil, err
	}
	var feed publicRSS
	if err := xml.Unmarshal(data, &feed); err != nil {
		return nil, fmt.Errorf("parse RSS: %w", err)
	}
	items := make([]indexedSearchItem, 0, len(feed.Channel.Items))
	for _, item := range feed.Channel.Items {
		title := cleanIndexText(item.Title)
		if title == "" {
			title = cleanIndexText(item.Description)
		}
		items = append(items, indexedSearchItem{Title: title, URL: strings.TrimSpace(html.UnescapeString(item.Link))})
	}
	return items, nil
}

func publicIndexDuckDuckGo(ctx context.Context, query string, limit int) ([]indexedSearchItem, error) {
	values := url.Values{}
	values.Set("q", query)
	endpoint := "https://lite.duckduckgo.com/lite/?" + values.Encode()
	data, err := publicGET(ctx, endpoint, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
	if err != nil {
		return nil, err
	}
	matches := anchorPattern.FindAllSubmatch(data, -1)
	items := make([]indexedSearchItem, 0, minInt(limit, len(matches)))
	for _, match := range matches {
		if len(match) < 3 {
			continue
		}
		rawURL := html.UnescapeString(string(match[1]))
		resolved := unwrapDuckDuckGoURL(rawURL)
		if resolved == "" {
			continue
		}
		title := cleanIndexText(string(match[2]))
		items = append(items, indexedSearchItem{Title: title, URL: resolved})
		if len(items) >= limit {
			break
		}
	}
	return items, nil
}

func publicGET(ctx context.Context, endpoint, accept string) ([]byte, error) {
	sem := publicSemaphore()
	select {
	case sem <- struct{}{}:
		defer func() { <-sem }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}

	timeout := envSeconds("PUBLIC_INDEX_TIMEOUT_SEC", 15)
	requestCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	userAgent := strings.TrimSpace(os.Getenv("PUBLIC_INDEX_USER_AGENT"))
	if userAgent == "" {
		userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept", accept)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, 3<<20))
}

func (p *PublicShortProvider) normalize(items []indexedSearchItem, keyword string, limit int) []model.Video {
	seen := map[string]struct{}{}
	results := make([]model.Video, 0, minInt(limit, len(items)))
	for _, item := range items {
		canonical := p.canonicalURL(item.URL)
		if canonical == "" {
			continue
		}
		if _, ok := seen[canonical]; ok {
			continue
		}
		seen[canonical] = struct{}{}
		title := cleanIndexText(item.Title)
		if title == "" {
			title = strings.Title(p.config.name) + " video"
		}
		results = append(results, model.Video{
			ID:           stablePublicID(canonical),
			Platform:     p.config.name,
			Title:        title,
			URL:          canonical,
			SearchSource: keyword,
		})
		if len(results) >= limit {
			break
		}
	}
	return results
}

func (p *PublicShortProvider) canonicalURL(raw string) string {
	raw = strings.TrimSpace(html.UnescapeString(raw))
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return ""
	}
	host := strings.ToLower(strings.TrimPrefix(u.Hostname(), "www."))
	allowed := false
	for _, suffix := range p.config.hosts {
		suffix = strings.ToLower(strings.TrimPrefix(suffix, "www."))
		if host == suffix || strings.HasSuffix(host, "."+suffix) {
			allowed = true
			break
		}
	}
	if !allowed || (p.config.acceptURL != nil && !p.config.acceptURL(u)) {
		return ""
	}
	u.Fragment = ""
	// Tracking parameters are not needed for downloading and make deduplication worse.
	query := u.Query()
	for key := range query {
		lower := strings.ToLower(key)
		if strings.HasPrefix(lower, "utm_") || lower == "spm" || lower == "from" || lower == "source" {
			query.Del(key)
		}
	}
	u.RawQuery = query.Encode()
	return u.String()
}

func unwrapDuckDuckGoURL(raw string) string {
	if strings.HasPrefix(raw, "//") {
		raw = "https:" + raw
	}
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	if strings.Contains(strings.ToLower(u.Hostname()), "duckduckgo.com") {
		if target := strings.TrimSpace(u.Query().Get("uddg")); target != "" {
			if decoded, err := url.QueryUnescape(target); err == nil {
				return decoded
			}
			return target
		}
		return ""
	}
	return raw
}

func cleanIndexText(value string) string {
	value = html.UnescapeString(value)
	value = tagPattern.ReplaceAllString(value, " ")
	value = strings.Join(strings.Fields(value), " ")
	return strings.TrimSpace(value)
}

func stablePublicID(value string) string {
	sum := sha1.Sum([]byte(value))
	return hex.EncodeToString(sum[:8])
}

func allDigits(value string) bool {
	if value == "" {
		return false
	}
	for _, r := range value {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func alphaNumeric(value string) bool {
	if value == "" {
		return false
	}
	for _, r := range value {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z')) {
			return false
		}
	}
	return true
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

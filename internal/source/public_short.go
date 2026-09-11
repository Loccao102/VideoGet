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

	"github.com/Loccao102/VideoGet/internal/model"
)

type publicShortConfig struct {
	name      string
	query     string
	hosts     []string
	acceptURL func(*url.URL) bool
}

type PublicShortProvider struct{ config publicShortConfig }

func NewPublicShortProviders() []Provider {
	configs := []publicShortConfig{
		{"kuaishou", "site:kuaishou.com/short-video", []string{"kuaishou.com"}, func(u *url.URL) bool {
			return strings.Contains(strings.ToLower(u.Path), "/short-video/")
		}},
		{"xiaohongshu", "site:xiaohongshu.com/explore", []string{"xiaohongshu.com"}, func(u *url.URL) bool {
			p := strings.ToLower(u.Path)
			return strings.Contains(p, "/explore/") || strings.Contains(p, "/discovery/item/")
		}},
		{"weibo", "site:weibo.com/tv/show", []string{"weibo.com"}, func(u *url.URL) bool {
			if strings.Contains(strings.ToLower(u.Path), "/tv/show/") {
				return true
			}
			parts := strings.Split(strings.Trim(u.Path, "/"), "/")
			return len(parts) == 2 && allDigits(parts[0]) && alphaNumeric(parts[1])
		}},
		{"xigua", "site:ixigua.com 视频", []string{"ixigua.com"}, func(u *url.URL) bool {
			p := strings.Trim(u.Path, "/")
			return len(p) >= 8 && allDigits(p)
		}},
		{"haokan", "site:haokan.baidu.com/v", []string{"haokan.baidu.com"}, func(u *url.URL) bool {
			return strings.TrimRight(strings.ToLower(u.Path), "/") == "/v" && strings.TrimSpace(u.Query().Get("vid")) != ""
		}},
		{"toutiao", "site:toutiao.com/video", []string{"toutiao.com"}, func(u *url.URL) bool {
			return strings.Contains(strings.ToLower(u.Path), "/video/")
		}},
		{"acfun", "site:acfun.cn/v/ac", []string{"acfun.cn"}, func(u *url.URL) bool {
			return strings.Contains(strings.ToLower(u.Path), "/v/ac")
		}},
		{"meipai", "site:meipai.com/media", []string{"meipai.com"}, func(u *url.URL) bool {
			return strings.Contains(strings.ToLower(u.Path), "/media/")
		}},
		{"weishi", "site:weishi.qq.com 视频", []string{"weishi.qq.com"}, func(u *url.URL) bool {
			p := strings.ToLower(u.Path)
			return strings.Contains(p, "/t/") || strings.Contains(p, "/video/")
		}},
	}
	providers := make([]Provider, 0, len(configs))
	for _, cfg := range configs {
		providers = append(providers, &PublicShortProvider{config: cfg})
	}
	return providers
}

func (p *PublicShortProvider) Name() string       { return p.config.name }
func (p *PublicShortProvider) Available() error   { return nil }

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

	var errs []string
	var videos []model.Video
	if items, err := publicIndexBing(ctx, query, limit); err != nil {
		errs = append(errs, "bing="+err.Error())
	} else {
		videos = appendUniqueVideos(videos, p.normalize(items, keyword, limit), limit)
	}

	// Search the second free index based on usable candidates, not raw result count.
	if len(videos) < limit {
		if items, err := publicIndexDuckDuckGo(ctx, query, limit); err != nil {
			errs = append(errs, "duckduckgo="+err.Error())
		} else {
			videos = appendUniqueVideos(videos, p.normalize(items, keyword, limit), limit)
		}
	}
	if len(videos) > 0 {
		return videos, nil
	}
	// No indexed candidates is normal. Only surface an error if both free indexes failed to execute.
	if len(errs) >= 2 {
		return nil, fmt.Errorf("%s public discovery unavailable: %s", p.config.name, strings.Join(errs, "; "))
	}
	return []model.Video{}, nil
}

type indexedSearchItem struct{ Title, URL string }

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
		concurrency := 6
		if raw := strings.TrimSpace(os.Getenv("PUBLIC_INDEX_CONCURRENCY")); raw != "" {
			if n, err := strconv.Atoi(raw); err == nil && n > 0 && n <= 16 {
				concurrency = n
			}
		}
		publicIndexSem = make(chan struct{}, concurrency)
	})
	return publicIndexSem
}

func publicIndexBing(ctx context.Context, query string, limit int) ([]indexedSearchItem, error) {
	values := url.Values{"format": {"rss"}, "count": {strconv.Itoa(limit)}, "q": {query}}
	data, err := publicGET(ctx, "https://www.bing.com/search?"+values.Encode(), "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.5")
	if err != nil {
		return nil, err
	}
	var feed publicRSS
	if err := xml.Unmarshal(data, &feed); err != nil {
		return nil, fmt.Errorf("parse RSS: %w", err)
	}
	out := make([]indexedSearchItem, 0, len(feed.Channel.Items))
	for _, item := range feed.Channel.Items {
		title := cleanIndexText(item.Title)
		if title == "" {
			title = cleanIndexText(item.Description)
		}
		out = append(out, indexedSearchItem{title, strings.TrimSpace(html.UnescapeString(item.Link))})
	}
	return out, nil
}

func publicIndexDuckDuckGo(ctx context.Context, query string, limit int) ([]indexedSearchItem, error) {
	values := url.Values{"q": {query}}
	data, err := publicGET(ctx, "https://lite.duckduckgo.com/lite/?"+values.Encode(), "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
	if err != nil {
		return nil, err
	}
	matches := anchorPattern.FindAllSubmatch(data, -1)
	out := make([]indexedSearchItem, 0, minInt(limit, len(matches)))
	for _, match := range matches {
		if len(match) < 3 {
			continue
		}
		resolved := unwrapDuckDuckGoURL(html.UnescapeString(string(match[1])))
		if resolved == "" {
			continue
		}
		out = append(out, indexedSearchItem{cleanIndexText(string(match[2])), resolved})
		if len(out) >= limit {
			break
		}
	}
	return out, nil
}

func publicGET(ctx context.Context, endpoint, accept string) ([]byte, error) {
	sem := publicSemaphore()
	select {
	case sem <- struct{}{}:
		defer func() { <-sem }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	requestCtx, cancel := context.WithTimeout(ctx, envSeconds("PUBLIC_INDEX_TIMEOUT_SEC", 10))
	defer cancel()
	req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	ua := strings.TrimSpace(os.Getenv("PUBLIC_INDEX_USER_AGENT"))
	if ua == "" {
		ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
	}
	req.Header.Set("User-Agent", ua)
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
	out := make([]model.Video, 0, minInt(limit, len(items)))
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
			title = p.config.name + " video"
		}
		out = append(out, model.Video{
			ID:           stablePublicID(canonical),
			Platform:     p.config.name,
			Title:        title,
			URL:          canonical,
			SearchSource: keyword,
		})
		if len(out) >= limit {
			break
		}
	}
	return out
}

func appendUniqueVideos(dst, src []model.Video, limit int) []model.Video {
	seen := make(map[string]struct{}, len(dst))
	for _, video := range dst {
		seen[video.URL] = struct{}{}
	}
	for _, video := range src {
		if _, ok := seen[video.URL]; ok {
			continue
		}
		seen[video.URL] = struct{}{}
		dst = append(dst, video)
		if len(dst) >= limit {
			break
		}
	}
	return dst
}

func (p *PublicShortProvider) canonicalURL(raw string) string {
	u, err := url.Parse(strings.TrimSpace(html.UnescapeString(raw)))
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
	q := u.Query()
	for key := range q {
		lower := strings.ToLower(key)
		if strings.HasPrefix(lower, "utm_") || lower == "spm" || lower == "from" || lower == "source" {
			q.Del(key)
		}
	}
	u.RawQuery = q.Encode()
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
		target := strings.TrimSpace(u.Query().Get("uddg"))
		if target == "" {
			return ""
		}
		if decoded, err := url.QueryUnescape(target); err == nil {
			return decoded
		}
		return target
	}
	return raw
}

func cleanIndexText(value string) string {
	value = html.UnescapeString(value)
	value = tagPattern.ReplaceAllString(value, " ")
	return strings.TrimSpace(strings.Join(strings.Fields(value), " "))
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

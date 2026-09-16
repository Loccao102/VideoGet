package source

import (
	"context"
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
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

// DouyinProvider deliberately has no platform-specific CLI dependency.
// Discovery first uses a public web index as a cheap fast path. When that does
// not fill the requested result count, it falls back to Douyin's own rendered
// search page through a real Chrome/Chromium browser.
type DouyinProvider struct{}

func NewDouyinProvider() *DouyinProvider { return &DouyinProvider{} }

func (p *DouyinProvider) Name() string { return "douyin" }

func (p *DouyinProvider) Available() error {
	// Public-index discovery only requires outbound HTTP access. Browser-native
	// discovery is a fallback, so the provider remains available even if Chrome
	// is temporarily missing or disabled.
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
	if limit > 50 {
		limit = 50
	}

	publicResults, publicErr := p.searchPublicIndex(ctx, keyword, limit)
	if len(publicResults) >= limit || !douyinNativeSearchEnabled() {
		if len(publicResults) > 0 {
			return publicResults[:minInt(limit, len(publicResults))], nil
		}
		if publicErr != nil {
			return nil, publicErr
		}
		return nil, fmt.Errorf("douyin discovery returned no video URLs for %q", keyword)
	}

	nativeResults, nativeErr := p.searchNativeBrowser(ctx, keyword, limit)
	merged := mergeDouyinSearchResults(publicResults, nativeResults, limit)
	if len(merged) > 0 {
		return merged, nil
	}
	if publicErr != nil && nativeErr != nil {
		return nil, fmt.Errorf("douyin discovery failed: public index: %v | native browser: %v", publicErr, nativeErr)
	}
	if nativeErr != nil {
		return nil, nativeErr
	}
	if publicErr != nil {
		return nil, publicErr
	}
	return nil, fmt.Errorf("douyin discovery returned no video URLs for %q", keyword)
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

	// DOUYIN_SEARCH_* are the new names. Keep the previous GUEST names as
	// compatibility aliases so an existing .env keeps working after this change.
	endpoint := firstNonEmpty(
		os.Getenv("DOUYIN_SEARCH_URL"),
		os.Getenv("DOUYIN_GUEST_SEARCH_URL"),
	)
	if endpoint == "" {
		values := url.Values{}
		values.Set("format", "rss")
		values.Set("count", strconv.Itoa(limit))
		values.Set("q", "site:douyin.com/video "+keyword)
		endpoint = "https://www.bing.com/search?" + values.Encode()
	}

	requestCtx, cancel := context.WithTimeout(ctx, douyinSearchTimeout())
	defer cancel()
	req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	userAgent := firstNonEmpty(
		os.Getenv("DOUYIN_USER_AGENT"),
		os.Getenv("DOUYIN_GUEST_USER_AGENT"),
	)
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
	results, err := parseGuestRSS(data, keyword, limit)
	if err != nil {
		return nil, err
	}
	if len(results) == 0 {
		return nil, fmt.Errorf("douyin public discovery returned no video URLs for %q", keyword)
	}
	return results, nil
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

func douyinSearchTimeout() time.Duration {
	for _, key := range []string{"DOUYIN_SEARCH_TIMEOUT_SEC", "DOUYIN_GUEST_TIMEOUT_SEC"} {
		value := strings.TrimSpace(os.Getenv(key))
		if value == "" {
			continue
		}
		seconds, err := strconv.Atoi(value)
		if err == nil && seconds > 0 && seconds <= 120 {
			return time.Duration(seconds) * time.Second
		}
	}
	return 20 * time.Second
}

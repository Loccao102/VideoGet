package source

import (
	"bytes"
	"context"
	"fmt"
	"html"
	"net/url"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"sync"

	"github.com/Loccao102/VideoGet/internal/model"
)

type KuaishouProvider struct {
	mu sync.Mutex
}

func NewKuaishouProvider() *KuaishouProvider { return &KuaishouProvider{} }
func (p *KuaishouProvider) Name() string { return "kuaishou" }
func (p *KuaishouProvider) Available() error { return nil }

var (
	kuaishouVideoPathPattern = regexp.MustCompile(`(?i)(?:https?://www\.kuaishou\.com)?/short-video/([a-z0-9]+)`)
	kuaishouAnchorPattern = regexp.MustCompile(`(?is)<a\b[^>]*href\s*=\s*["']([^"']*/short-video/[a-z0-9]+[^"']*)["'][^>]*>(.*?)</a>`)
)

func (p *KuaishouProvider) Search(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	keyword = strings.TrimSpace(keyword)
	if keyword == "" {
		return nil, fmt.Errorf("kuaishou keyword is required")
	}
	if limit <= 0 {
		limit = 10
	}
	if limit > 50 {
		limit = 50
	}

	public := p.searchPublic(ctx, keyword, limit)
	if len(public) >= limit || !envSourceBool("KUAISHOU_NATIVE_SEARCH", true) {
		return public, nil
	}

	native, err := p.searchBrowser(ctx, keyword, limit)
	merged := mergeDouyinSearchResults(public, native, limit)
	if len(merged) > 0 {
		return merged, nil
	}
	if err != nil {
		return nil, err
	}
	return nil, fmt.Errorf("kuaishou discovery returned no video URLs for %q", keyword)
}

func (p *KuaishouProvider) searchPublic(ctx context.Context, keyword string, limit int) []model.Video {
	query := "site:kuaishou.com/short-video " + keyword
	var out []model.Video
	for _, search := range []func(context.Context, string, int) ([]indexedSearchItem, error){publicIndexBing, publicIndexDuckDuckGo} {
		items, err := search(ctx, query, limit)
		if err != nil {
			continue
		}
		for _, item := range items {
			u := canonicalKuaishouVideoURL(item.URL)
			if u == "" {
				continue
			}
			id := extractKuaishouVideoID(u)
			if id == "" {
				continue
			}
			title := cleanIndexText(item.Title)
			if title == "" {
				title = "Kuaishou video " + id
			}
			out = appendUniqueVideos(out, []model.Video{{
				ID: id, Platform: "kuaishou", Title: title, URL: u, MediaType: "video", SearchSource: keyword,
			}}, limit)
			if len(out) >= limit {
				return out
			}
		}
	}
	return out
}

func (p *KuaishouProvider) searchBrowser(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	browser, err := findDouyinSearchBrowserBinary()
	if err != nil {
		return nil, fmt.Errorf("kuaishou native search browser: %w", err)
	}
	timeout := envSeconds("KUAISHOU_NATIVE_SEARCH_TIMEOUT_SEC", 35)
	browserCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	profile, err := os.MkdirTemp("", "videoget-kuaishou-search-*")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(profile)

	pageURL := "https://www.kuaishou.com/search/video?searchKey=" + url.QueryEscape(keyword)
	args := []string{
		"--headless=new",
		"--disable-gpu",
		"--disable-dev-shm-usage",
		"--no-first-run",
		"--no-default-browser-check",
		"--mute-audio",
		"--hide-scrollbars",
		"--window-size=1440,1800",
		fmt.Sprintf("--virtual-time-budget=%d", envPositiveSourceInt("KUAISHOU_NATIVE_SEARCH_RENDER_MS", 10000)),
		"--user-data-dir=" + profile,
	}
	if envSourceBool("DOUYIN_BROWSER_NO_SANDBOX", false) {
		args = append(args, "--no-sandbox")
	}
	args = append(args, "--dump-dom", pageURL)

	cmd := exec.CommandContext(browserCtx, browser, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if browserCtx.Err() != nil {
			return nil, fmt.Errorf("kuaishou native search timed out after %s", timeout)
		}
		msg := strings.TrimSpace(stderr.String())
		if len(msg) > 700 {
			msg = msg[len(msg)-700:]
		}
		return nil, fmt.Errorf("kuaishou native search failed: %v: %s", err, msg)
	}
	return parseKuaishouSearchDOM(stdout.String(), keyword, limit), nil
}

func parseKuaishouSearchDOM(document, keyword string, limit int) []model.Video {
	if limit <= 0 {
		limit = 10
	}
	document = html.UnescapeString(document)
	document = strings.ReplaceAll(document, `\/`, "/")
	seen := map[string]struct{}{}
	out := make([]model.Video, 0, limit)

	appendID := func(id, title string) {
		if id == "" || len(out) >= limit {
			return
		}
		if _, ok := seen[id]; ok {
			return
		}
		seen[id] = struct{}{}
		title = cleanIndexText(title)
		if title == "" {
			title = "Kuaishou search: " + keyword
		}
		out = append(out, model.Video{
			ID: id, Platform: "kuaishou", Title: title,
			URL: "https://www.kuaishou.com/short-video/" + id,
			MediaType: "video", SearchSource: keyword,
		})
	}

	for _, m := range kuaishouAnchorPattern.FindAllStringSubmatch(document, -1) {
		if len(m) != 3 {
			continue
		}
		appendID(extractKuaishouVideoID(m[1]), m[2])
		if len(out) >= limit {
			return out
		}
	}
	for _, m := range kuaishouVideoPathPattern.FindAllStringSubmatch(document, -1) {
		if len(m) == 2 {
			appendID(m[1], "")
		}
		if len(out) >= limit {
			break
		}
	}
	return out
}

func extractKuaishouVideoID(raw string) string {
	m := kuaishouVideoPathPattern.FindStringSubmatch(raw)
	if len(m) != 2 {
		return ""
	}
	return strings.TrimSpace(m[1])
}

func canonicalKuaishouVideoURL(raw string) string {
	id := extractKuaishouVideoID(raw)
	if id == "" {
		return ""
	}
	return "https://www.kuaishou.com/short-video/" + id
}


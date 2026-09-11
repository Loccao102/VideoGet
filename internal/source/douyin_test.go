package source

import (
	"strings"
	"testing"
)

func TestParseGuestRSS(t *testing.T) {
	rss := `<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>厨房神器分享 - 抖音</title>
      <link>https://www.douyin.com/video/7381234567890123456</link>
      <description>厨房神器</description>
    </item>
    <item>
      <title>duplicate</title>
      <link>https://www.douyin.com/video/7381234567890123456?foo=1</link>
    </item>
    <item>
      <title>not douyin</title>
      <link>https://example.com/watch/12345678</link>
    </item>
  </channel>
</rss>`

	videos, err := parseGuestRSS([]byte(rss), "厨房神器", 10)
	if err != nil {
		t.Fatalf("parseGuestRSS: %v", err)
	}
	if len(videos) != 1 {
		t.Fatalf("len(videos) = %d, want 1", len(videos))
	}
	video := videos[0]
	if video.ID != "7381234567890123456" {
		t.Fatalf("ID = %q", video.ID)
	}
	if video.Title != "厨房神器分享" {
		t.Fatalf("Title = %q", video.Title)
	}
	if video.Platform != "douyin" {
		t.Fatalf("Platform = %q", video.Platform)
	}
	if video.SearchSource != "厨房神器" {
		t.Fatalf("SearchSource = %q", video.SearchSource)
	}
}

func TestDouyinCommandEnvGuestStripsCookie(t *testing.T) {
	t.Setenv("DOUYIN_COOKIE", "secret-cookie")
	env := douyinCommandEnv("")
	for _, item := range env {
		if strings.HasPrefix(item, "DOUYIN_COOKIE=") {
			t.Fatalf("guest env leaked cookie: %q", item)
		}
	}
}

func TestDouyinCommandEnvAuthenticatedSetsCookie(t *testing.T) {
	t.Setenv("DOUYIN_COOKIE", "old-cookie")
	env := douyinCommandEnv("new-cookie")
	count := 0
	for _, item := range env {
		if strings.HasPrefix(item, "DOUYIN_COOKIE=") {
			count++
			if item != "DOUYIN_COOKIE=new-cookie" {
				t.Fatalf("unexpected cookie env: %q", item)
			}
		}
	}
	if count != 1 {
		t.Fatalf("cookie env count = %d, want 1", count)
	}
}

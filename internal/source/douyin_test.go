package source

import "testing"

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

func TestDouyinProviderDoesNotRequireCLI(t *testing.T) {
	t.Setenv("PATH", "")
	provider := NewDouyinProvider()
	if err := provider.Available(); err != nil {
		t.Fatalf("Available() = %v, want nil without douyin-cli", err)
	}
}

func TestExtractDouyinVideoID(t *testing.T) {
	got := extractDouyinVideoID("https://www.douyin.com/video/7664188112177079482?previous_page=web_code_link")
	if got != "7664188112177079482" {
		t.Fatalf("extractDouyinVideoID() = %q", got)
	}
}

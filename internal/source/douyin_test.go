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
	if video.MediaType != "video" {
		t.Fatalf("MediaType = %q", video.MediaType)
	}
	if video.SearchSource != "厨房神器" {
		t.Fatalf("SearchSource = %q", video.SearchSource)
	}
}

func TestParseDouyinBrowserMetadataIncludesViews(t *testing.T) {
	raw := `[{"id":"7685323803187284187","desc":"browser result","author_nickname":"tester","cover":"https://example.com/cover.jpg","duration":12500,"play_count":987654,"digg_count":12345,"comment_count":88,"share_count":77,"download_addr":"https://example.com/video.mp4","time":1789440000,"search_source":"法令纹"}]`
	videos, err := parseDouyinMetadata([]byte(raw), "fallback")
	if err != nil {
		t.Fatalf("parseDouyinMetadata: %v", err)
	}
	if len(videos) != 1 {
		t.Fatalf("len(videos) = %d, want 1", len(videos))
	}
	video := videos[0]
	if video.Views != 987654 {
		t.Fatalf("Views = %d, want 987654", video.Views)
	}
	if video.DurationSec != 12 {
		t.Fatalf("DurationSec = %d, want 12", video.DurationSec)
	}
	if video.DownloadURL != "https://example.com/video.mp4" {
		t.Fatalf("DownloadURL = %q", video.DownloadURL)
	}
	if video.SearchSource != "法令纹" {
		t.Fatalf("SearchSource = %q", video.SearchSource)
	}
	if video.MediaType != "video" {
		t.Fatalf("MediaType = %q", video.MediaType)
	}
}

func TestDouyinLegacyModesMapToBrowser(t *testing.T) {
	for _, mode := range []string{"", "auto", "authenticated"} {
		t.Run(mode, func(t *testing.T) {
			t.Setenv("DOUYIN_MODE", mode)
			provider := NewDouyinProvider()
			if provider.mode != "browser" {
				t.Fatalf("mode = %q, want browser", provider.mode)
			}
		})
	}
}

func TestDouyinGuestModeMapsToPublic(t *testing.T) {
	t.Setenv("DOUYIN_MODE", "guest")
	provider := NewDouyinProvider()
	if provider.mode != "public" {
		t.Fatalf("mode = %q, want public", provider.mode)
	}
}

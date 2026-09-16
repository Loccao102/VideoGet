package download

import "testing"

func TestNormalizeDouyinBrowserCandidates(t *testing.T) {
	got := normalizeDouyinBrowserCandidates([]string{
		"blob:https://www.douyin.com/abc",
		"https://v26-web.douyinvod.com/token/video/tos/cn/example/?mime_type=video_mp4&amp;br=1000",
		"https://v26-web.douyinvod.com/token/video/tos/cn/example/?mime_type=video_mp4&amp;br=1000",
		"http://www.douyin.com/aweme/v1/play/?video_id=123",
		"https://example.com/file.txt",
	})
	if len(got) != 2 {
		t.Fatalf("len(got) = %d, want 2: %#v", len(got), got)
	}
	if got[0] != "https://v26-web.douyinvod.com/token/video/tos/cn/example/?mime_type=video_mp4&br=1000" {
		t.Fatalf("unexpected first candidate: %q", got[0])
	}
	if got[1] != "https://www.douyin.com/aweme/v1/play/?video_id=123" {
		t.Fatalf("unexpected second candidate: %q", got[1])
	}
}

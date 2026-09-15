package download

import (
	"os"
	"strings"
	"testing"
)

func TestDouyinMediaCandidates(t *testing.T) {
	document := `<html><body>
<video src="https://v.douyinvod.com/abc/video.mp4?x=1&amp;y=2"></video>
<script>window.__DATA__={"play":"https:\/\/v.douyinvod.com\/def\/play\/?mime_type=video_mp4"}</script>
<img src="https://example.com/image.jpg">
</body></html>`

	candidates := douyinMediaCandidates(document)
	if len(candidates) != 2 {
		t.Fatalf("len(candidates) = %d, want 2: %#v", len(candidates), candidates)
	}
	if candidates[0] != "https://v.douyinvod.com/abc/video.mp4?x=1&y=2" {
		t.Fatalf("first candidate = %q", candidates[0])
	}
	if candidates[1] != "https://v.douyinvod.com/def/play/?mime_type=video_mp4" {
		t.Fatalf("second candidate = %q", candidates[1])
	}
}

func TestWriteDouyinCookieFile(t *testing.T) {
	path, cleanup, err := writeDouyinCookieFile("ttwid=abc123; sessionid=xyz=456; malformed; s_v_web_id=verify_1")
	if err != nil {
		t.Fatalf("writeDouyinCookieFile: %v", err)
	}
	defer cleanup()

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read cookie file: %v", err)
	}
	text := string(data)
	for _, expected := range []string{
		"# Netscape HTTP Cookie File",
		".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\tabc123",
		".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\txyz=456",
		".douyin.com\tTRUE\t/\tTRUE\t0\ts_v_web_id\tverify_1",
	} {
		if !strings.Contains(text, expected) {
			t.Fatalf("cookie file missing %q:\n%s", expected, text)
		}
	}
	if strings.Contains(text, "malformed") {
		t.Fatalf("malformed cookie token should be ignored:\n%s", text)
	}
}

func TestWriteDouyinCookieFileEmpty(t *testing.T) {
	path, cleanup, err := writeDouyinCookieFile("")
	defer cleanup()
	if err != nil {
		t.Fatalf("empty cookie returned error: %v", err)
	}
	if path != "" {
		t.Fatalf("path = %q, want empty", path)
	}
}

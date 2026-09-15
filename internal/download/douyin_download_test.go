package download

import (
	"strings"
	"testing"
)

func TestDouyinRouterDataCandidates(t *testing.T) {
	document := `<html><body><script>
window._ROUTER_DATA = {
  "loaderData": {
    "video_(7664188112177079482)/page": {
      "videoInfoRes": {
        "item_list": [{
          "aweme_id": "7664188112177079482",
          "video": {
            "play_addr": {
              "uri": "v0200fg10000example",
              "url_list": ["https://v3-web.douyinvod.com/base/playwm/?mime_type=video_mp4"]
            },
            "bit_rate": [
              {"bit_rate": 1200000, "play_addr": {"url_list": ["https://v3-web.douyinvod.com/low/video.mp4"]}},
              {"bit_rate": 3600000, "play_addr": {"url_list": ["https://v3-web.douyinvod.com/high/video.mp4"]}}
            ]
          }
        }]
      }
    }
  }
};
</script></body></html>`

	candidates, err := douyinRouterDataCandidates(document)
	if err != nil {
		t.Fatalf("douyinRouterDataCandidates: %v", err)
	}
	if len(candidates) < 7 {
		t.Fatalf("len(candidates) = %d, want at least 7: %#v", len(candidates), candidates)
	}
	if candidates[0] != "https://v3-web.douyinvod.com/high/video.mp4" {
		t.Fatalf("highest bitrate candidate = %q", candidates[0])
	}
	if candidates[1] != "https://v3-web.douyinvod.com/low/video.mp4" {
		t.Fatalf("second candidate = %q", candidates[1])
	}
	if candidates[2] != "https://v3-web.douyinvod.com/base/play/?mime_type=video_mp4" {
		t.Fatalf("playwm was not normalized: %q", candidates[2])
	}
	if !strings.Contains(candidates[3], "aweme.snssdk.com/aweme/v1/play/") || !strings.Contains(candidates[3], "ratio=1080p") {
		t.Fatalf("missing 1080p URI fallback: %q", candidates[3])
	}
}

func TestDouyinRouterDataEmptyItemReportsUnavailable(t *testing.T) {
	document := `<script>window._ROUTER_DATA={"loaderData":{"x":{"videoInfoRes":{"item_list":[],"filter_list":[{"detail_msg":"作品已删除"}]}}}};</script>`
	_, err := douyinRouterDataCandidates(document)
	if err == nil || !strings.Contains(err.Error(), "作品已删除") {
		t.Fatalf("error = %v, want unavailable detail", err)
	}
}

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

func TestExtractDouyinDownloadVideoID(t *testing.T) {
	cases := map[string]string{
		"https://www.douyin.com/video/7664188112177079482":                         "7664188112177079482",
		"https://www.iesdouyin.com/share/video/7664188112177079482/":               "7664188112177079482",
		"https://www.douyin.com/user/foo?modal_id=7664188112177079482&showTab=post": "7664188112177079482",
	}
	for input, want := range cases {
		if got := extractDouyinDownloadVideoID(input); got != want {
			t.Fatalf("extractDouyinDownloadVideoID(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestExtractDouyinJSONObjectHandlesNestedStrings(t *testing.T) {
	document := `prefix {"a":{"text":"brace } inside string","escaped":"quote \\" ok"},"b":1}; suffix`
	start := strings.Index(document, "{")
	got := extractDouyinJSONObject(document, start)
	if !strings.HasSuffix(got, `"b":1}`) {
		t.Fatalf("unexpected JSON object: %q", got)
	}
}

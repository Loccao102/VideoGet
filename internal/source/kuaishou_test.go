package source

import "testing"

func TestParseKuaishouSearchDOMExtractsShortVideos(t *testing.T) {
	document := `
	<html><body>
		<a href="/short-video/3xabc123">猫咪迷惑行为合集</a>
		<a href="https://www.kuaishou.com/short-video/3xdef456?foo=bar">狗狗搞笑</a>
		<script>{"url":"https:\/\/www.kuaishou.com\/short-video\/3xghi789"}</script>
	</body></html>`
	results := parseKuaishouSearchDOM(document, "猫咪迷惑行为", 10)
	if len(results) != 3 {
		t.Fatalf("len(results)=%d, want 3", len(results))
	}
	if results[0].ID != "3xabc123" || results[0].Platform != "kuaishou" {
		t.Fatalf("unexpected first result: %+v", results[0])
	}
	if results[1].URL != "https://www.kuaishou.com/short-video/3xdef456" {
		t.Fatalf("unexpected canonical URL: %s", results[1].URL)
	}
}

func TestCanonicalKuaishouVideoURL(t *testing.T) {
	got := canonicalKuaishouVideoURL("https://www.kuaishou.com/short-video/3xabc123?utm_source=x")
	want := "https://www.kuaishou.com/short-video/3xabc123"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

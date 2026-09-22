package download

import "testing"

func TestExtractKuaishouMediaURLFromInitState(t *testing.T) {
	body := []byte(`<html><script>window.INIT_STATE = {"item":{"photo":{"photoType":"VIDEO","mainMvUrls":[{"url":"https://example.kwaicdn.com/video.mp4"}]}}};</script></html>`)
	got := extractKuaishouMediaURL(body)
	if got != "https://example.kwaicdn.com/video.mp4" {
		t.Fatalf("got %q", got)
	}
}

func TestExtractKuaishouMediaURLFromApolloState(t *testing.T) {
	body := []byte(`<script>window.__APOLLO_STATE__ = {"defaultClient":{"VisionVideoDetailPhoto:3xabc":{"photoUrl":"https://example.kwaicdn.com/original.mp4"}}};</script>`)
	got := extractKuaishouMediaURL(body)
	if got != "https://example.kwaicdn.com/original.mp4" {
		t.Fatalf("got %q", got)
	}
}

func TestExtractKuaishouMediaURLRegexFallback(t *testing.T) {
	body := []byte(`<script>window.INIT_STATE = {broken:true,"photoUrl":"https:\/\/example.kwaicdn.com\/fallback.mp4"};</script>`)
	got := extractKuaishouMediaURL(body)
	if got != "https://example.kwaicdn.com/fallback.mp4" {
		t.Fatalf("got %q", got)
	}
}

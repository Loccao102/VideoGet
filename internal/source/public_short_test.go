package source

import "testing"

func TestPublicShortProvidersNormalizeSupportedURLs(t *testing.T) {
	providers := NewPublicShortProviders()
	if len(providers) < 8 {
		t.Fatalf("got %d providers, want at least 8", len(providers))
	}

	cases := map[string]string{
		"kuaishou":     "https://www.kuaishou.com/short-video/3xabc123?utm_source=test",
		"xiaohongshu": "https://www.xiaohongshu.com/explore/66abcdef1234567890",
		"weibo":        "https://weibo.com/tv/show/1034:1234567890",
		"xigua":        "https://www.ixigua.com/7123456789012345678/",
		"haokan":       "https://haokan.baidu.com/v?vid=1234567890123456789",
		"toutiao":      "https://www.toutiao.com/video/7123456789012345678/",
		"acfun":        "https://www.acfun.cn/v/ac12345678",
		"meipai":       "https://www.meipai.com/media/1234567890",
		"weishi":       "https://weishi.qq.com/t/abc123",
	}

	byName := map[string]*PublicShortProvider{}
	for _, provider := range providers {
		p, ok := provider.(*PublicShortProvider)
		if !ok {
			t.Fatalf("unexpected provider type %T", provider)
		}
		byName[p.Name()] = p
	}

	for name, rawURL := range cases {
		provider := byName[name]
		if provider == nil {
			t.Fatalf("provider %s missing", name)
		}
		canonical := provider.canonicalURL(rawURL)
		if canonical == "" {
			t.Errorf("provider %s rejected valid URL %s", name, rawURL)
		}
	}
}

func TestPublicShortProviderRejectsWrongHost(t *testing.T) {
	providers := NewPublicShortProviders()
	for _, provider := range providers {
		p := provider.(*PublicShortProvider)
		if got := p.canonicalURL("https://example.com/video/123456789"); got != "" {
			t.Fatalf("provider %s accepted unrelated host: %s", p.Name(), got)
		}
	}
}

func TestUnwrapDuckDuckGoURL(t *testing.T) {
	got := unwrapDuckDuckGoURL("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.kuaishou.com%2Fshort-video%2Fabc")
	want := "https://www.kuaishou.com/short-video/abc"
	if got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

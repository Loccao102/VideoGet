package source

import (
	"testing"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

func TestParseDouyinSearchDOMExtractsRenderedAndHydratedVideos(t *testing.T) {
	document := `
<html><body>
<a href="/video/7654321098765432101"><span>无线吸尘器真实测评</span></a>
<script>
self.__pace_f.push([1,"{\"aweme_id\":\"7654321098765432101\",\"desc\":\"吸尘器测评：适合小户型\",\"author\":{\"nickname\":\"家电实验室\"},\"statistics\":{\"play_count\":120000,\"digg_count\":9300,\"comment_count\":420,\"share_count\":188},\"create_time\":1789000000,\"video\":{\"duration\":42000}}"]);
self.__pace_f.push([1,"{\"aweme_id\":\"7654321098765432102\",\"desc\":\"第二个搜索结果\"}"]);
</script>
</body></html>`

	results := parseDouyinSearchDOM(document, "无线吸尘器", 10)
	if len(results) != 2 {
		t.Fatalf("len(results) = %d, want 2", len(results))
	}
	first := results[0]
	if first.ID != "7654321098765432101" {
		t.Fatalf("first ID = %q", first.ID)
	}
	if first.Title == "" {
		t.Fatalf("first title should be populated")
	}
	if first.Platform != "douyin" || first.MediaType != "video" {
		t.Fatalf("unexpected platform/media type: %q/%q", first.Platform, first.MediaType)
	}
	if first.URL != "https://www.douyin.com/video/7654321098765432101" {
		t.Fatalf("first URL = %q", first.URL)
	}
	if first.SearchSource != "无线吸尘器" {
		t.Fatalf("SearchSource = %q", first.SearchSource)
	}
}

func TestEnrichDouyinNativeResultReadsNearbyMetadata(t *testing.T) {
	document := `{"aweme_id":"7654321098765432101","desc":"吸尘器测评","nickname":"家电实验室","play_count":120000,"digg_count":9300,"comment_count":420,"share_count":188,"create_time":1789000000,"duration":42000}`
	video := model.Video{ID: "7654321098765432101"}
	enrichDouyinNativeResult(&video, document)

	if video.Title != "吸尘器测评" || video.Author != "家电实验室" {
		t.Fatalf("metadata text = %q / %q", video.Title, video.Author)
	}
	if video.Views != 120000 || video.Likes != 9300 || video.Comments != 420 || video.Shares != 188 {
		t.Fatalf("stats = views:%d likes:%d comments:%d shares:%d", video.Views, video.Likes, video.Comments, video.Shares)
	}
	if video.DurationSec != 42 {
		t.Fatalf("DurationSec = %d, want 42", video.DurationSec)
	}
	wantPublished := time.Unix(1789000000, 0).UTC()
	if video.PublishedAt == nil || !video.PublishedAt.Equal(wantPublished) {
		t.Fatalf("PublishedAt = %v, want %v", video.PublishedAt, wantPublished)
	}
}

func TestMergeDouyinSearchResultsKeepsFastPathThenFillsNative(t *testing.T) {
	public := []model.Video{
		{ID: "1", Platform: "douyin", Title: "public 1"},
		{ID: "2", Platform: "douyin", Title: "public 2"},
	}
	native := []model.Video{
		{ID: "2", Platform: "douyin", Title: "duplicate"},
		{ID: "3", Platform: "douyin", Title: "native 3"},
		{ID: "4", Platform: "douyin", Title: "native 4"},
	}
	merged := mergeDouyinSearchResults(public, native, 4)
	if len(merged) != 4 {
		t.Fatalf("len(merged) = %d, want 4", len(merged))
	}
	want := []string{"1", "2", "3", "4"}
	for i, id := range want {
		if merged[i].ID != id {
			t.Fatalf("merged[%d].ID = %q, want %q", i, merged[i].ID, id)
		}
	}
}

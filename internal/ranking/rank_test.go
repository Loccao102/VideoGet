package ranking

import (
	"testing"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

func TestDeduplicateAndRank(t *testing.T) {
	now := time.Date(2026, 9, 10, 12, 0, 0, 0, time.UTC)
	recent := now.Add(-6 * time.Hour)
	old := now.Add(-30 * 24 * time.Hour)
	videos := []model.Video{
		{ID: "1", Platform: "douyin", Title: "厨房好物", Likes: 5000, Comments: 300, Shares: 200, PublishedAt: &recent, SearchSource: "厨房好物"},
		{ID: "1", Platform: "douyin", Title: "厨房好物", Likes: 7000, Comments: 300, Shares: 200, PublishedAt: &recent, SearchSource: "厨房神器"},
		{ID: "2", Platform: "douyin", Title: "普通视频", Likes: 9000, Comments: 100, Shares: 50, PublishedAt: &old, SearchSource: "đồ bếp"},
	}

	videos = Deduplicate(videos)
	if len(videos) != 2 {
		t.Fatalf("expected 2 unique videos, got %d", len(videos))
	}
	if videos[0].Likes != 7000 {
		t.Fatalf("expected merged max likes, got %d", videos[0].Likes)
	}

	ranked := Rank(videos, "đồ bếp", "rank", model.SearchFilters{}, now)
	if len(ranked) != 2 || ranked[0].Scores == nil {
		t.Fatalf("ranking did not populate scores: %+v", ranked)
	}
	if ranked[0].Scores.Overall < ranked[1].Scores.Overall {
		t.Fatalf("results are not sorted by overall score")
	}
}

func TestFilters(t *testing.T) {
	now := time.Now().UTC()
	recent := now.Add(-24 * time.Hour)
	videos := []model.Video{
		{ID: "1", Platform: "bilibili", Likes: 100, DurationSec: 30, PublishedAt: &recent},
		{ID: "2", Platform: "bilibili", Likes: 2, DurationSec: 300, PublishedAt: &recent},
	}
	got := Rank(videos, "x", "rank", model.SearchFilters{MinLikes: 10, MaxDurationSec: 60}, now)
	if len(got) != 1 || got[0].ID != "1" {
		t.Fatalf("unexpected filtered results: %+v", got)
	}
}

package model

import "time"

type VideoScores struct {
	Engagement float64 `json:"engagement"`
	Recency    float64 `json:"recency"`
	Relevance  float64 `json:"relevance"`
	Affiliate  float64 `json:"affiliate"`
	Trend      float64 `json:"trend"`
	Overall    float64 `json:"overall"`
}

type Video struct {
	ID           string       `json:"id"`
	Platform     string       `json:"platform"`
	Title        string       `json:"title"`
	Author       string       `json:"author,omitempty"`
	URL          string       `json:"url"`
	Thumbnail    string       `json:"thumbnail,omitempty"`
	// MediaType is "video", "image", or empty/"unknown" when public discovery
	// has not verified the underlying post yet.
	MediaType    string       `json:"mediaType,omitempty"`
	DurationSec  int64        `json:"durationSec,omitempty"`
	Views        int64        `json:"views,omitempty"`
	Likes        int64        `json:"likes,omitempty"`
	Comments     int64        `json:"comments,omitempty"`
	Shares       int64        `json:"shares,omitempty"`
	PublishedAt  *time.Time   `json:"publishedAt,omitempty"`
	DownloadURL  string       `json:"downloadUrl,omitempty"`
	SearchSource string       `json:"searchSource,omitempty"`
	Scores       *VideoScores `json:"scores,omitempty"`
}

type SearchFilters struct {
	MinViews            int64 `json:"minViews,omitempty"`
	MinLikes            int64 `json:"minLikes,omitempty"`
	MaxDurationSec      int64 `json:"maxDurationSec,omitempty"`
	PublishedWithinDays int   `json:"publishedWithinDays,omitempty"`
}

type SearchRequest struct {
	Keyword string        `json:"keyword"`
	Sources []string      `json:"sources"`
	Limit   int           `json:"limit"`
	Expand  bool          `json:"expand"`
	Sort    string        `json:"sort,omitempty"`
	Filters SearchFilters `json:"filters,omitempty"`
}

type SearchResponse struct {
	Keyword  string            `json:"keyword"`
	Keywords []string          `json:"keywords"`
	Results  []Video           `json:"results"`
	Errors   map[string]string `json:"errors,omitempty"`
}

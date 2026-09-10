package model

import "time"

type Video struct {
	ID string `json:"id"`
	Platform string `json:"platform"`
	Title string `json:"title"`
	Author string `json:"author,omitempty"`
	URL string `json:"url"`
	Thumbnail string `json:"thumbnail,omitempty"`
	DurationSec int64 `json:"durationSec,omitempty"`
	Views int64 `json:"views,omitempty"`
	Likes int64 `json:"likes,omitempty"`
	Comments int64 `json:"comments,omitempty"`
	Shares int64 `json:"shares,omitempty"`
	PublishedAt *time.Time `json:"publishedAt,omitempty"`
	DownloadURL string `json:"downloadUrl,omitempty"`
	SearchSource string `json:"searchSource,omitempty"`
}

type SearchRequest struct { Keyword string `json:"keyword"`; Sources []string `json:"sources"`; Limit int `json:"limit"` }
type SearchResponse struct { Keyword string `json:"keyword"`; Results []Video `json:"results"`; Errors map[string]string `json:"errors,omitempty"` }

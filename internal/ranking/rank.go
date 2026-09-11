package ranking

import (
	"math"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/Loccao102/VideoGet/internal/model"
)

func Rank(videos []model.Video, query, sortBy string, filters model.SearchFilters, now time.Time) []model.Video {
	filtered := make([]model.Video, 0, len(videos))
	for _, video := range videos {
		if !passes(video, filters, now) {
			continue
		}
		filtered = append(filtered, video)
	}
	if len(filtered) == 0 {
		return filtered
	}

	engagementRaw := make([]float64, len(filtered))
	velocityRaw := make([]float64, len(filtered))
	maxEngagement, maxVelocity := 0.0, 0.0
	for i, video := range filtered {
		engagementRaw[i] = math.Log1p(float64(video.Likes) + 2*float64(video.Comments) + 3*float64(video.Shares) + 0.01*float64(video.Views))
		if engagementRaw[i] > maxEngagement {
			maxEngagement = engagementRaw[i]
		}
		ageHours := 24.0 * 30.0
		if video.PublishedAt != nil {
			ageHours = math.Max(1, now.Sub(*video.PublishedAt).Hours())
		}
		velocityRaw[i] = engagementRaw[i] / ageHours
		if velocityRaw[i] > maxVelocity {
			maxVelocity = velocityRaw[i]
		}
	}

	for i := range filtered {
		engagement := normalize(engagementRaw[i], maxEngagement)
		velocity := normalize(velocityRaw[i], maxVelocity)
		recency := recencyScore(filtered[i].PublishedAt, now)
		relevance := relevanceScore(filtered[i], query)
		affiliate := affiliateScore(filtered[i])
		trend := clamp01(0.45*engagement + 0.35*recency + 0.20*velocity)
		// Affiliate discovery intentionally gives commerce intent/source fit enough weight
		// to keep sparse public-index candidates competitive with high-engagement long-form videos.
		overall := clamp01(0.32*trend + 0.24*relevance + 0.14*engagement + 0.30*affiliate)
		filtered[i].Scores = &model.VideoScores{
			Engagement: round(engagement * 100),
			Recency:    round(recency * 100),
			Relevance:  round(relevance * 100),
			Affiliate:  round(affiliate * 100),
			Trend:      round(trend * 100),
			Overall:    round(overall * 100),
		}
	}

	switch strings.ToLower(strings.TrimSpace(sortBy)) {
	case "latest":
		sort.SliceStable(filtered, func(i, j int) bool { return publishedUnix(filtered[i]) > publishedUnix(filtered[j]) })
	case "likes":
		sort.SliceStable(filtered, func(i, j int) bool { return filtered[i].Likes > filtered[j].Likes })
	case "views":
		sort.SliceStable(filtered, func(i, j int) bool { return filtered[i].Views > filtered[j].Views })
	case "affiliate":
		sort.SliceStable(filtered, func(i, j int) bool { return filtered[i].Scores.Affiliate > filtered[j].Scores.Affiliate })
	default:
		sort.SliceStable(filtered, func(i, j int) bool { return filtered[i].Scores.Overall > filtered[j].Scores.Overall })
	}
	return filtered
}

func Deduplicate(videos []model.Video) []model.Video {
	out := make([]model.Video, 0, len(videos))
	seen := map[string]int{}
	for _, video := range videos {
		key := strings.ToLower(strings.TrimSpace(video.Platform)) + "|" + strings.TrimSpace(video.ID)
		if key == "|" || strings.HasSuffix(key, "|") {
			key = strings.ToLower(strings.TrimSpace(video.Platform)) + "|" + strings.TrimSpace(video.URL)
		}
		if idx, ok := seen[key]; ok {
			out[idx] = merge(out[idx], video)
			continue
		}
		seen[key] = len(out)
		out = append(out, video)
	}
	return out
}

func merge(a, b model.Video) model.Video {
	if a.Title == "" { a.Title = b.Title }
	if a.Author == "" { a.Author = b.Author }
	if a.URL == "" { a.URL = b.URL }
	if a.Thumbnail == "" { a.Thumbnail = b.Thumbnail }
	if a.DurationSec == 0 { a.DurationSec = b.DurationSec }
	if b.Views > a.Views { a.Views = b.Views }
	if b.Likes > a.Likes { a.Likes = b.Likes }
	if b.Comments > a.Comments { a.Comments = b.Comments }
	if b.Shares > a.Shares { a.Shares = b.Shares }
	if a.PublishedAt == nil || (b.PublishedAt != nil && b.PublishedAt.After(*a.PublishedAt)) { a.PublishedAt = b.PublishedAt }
	if a.DownloadURL == "" { a.DownloadURL = b.DownloadURL }
	if a.SearchSource == "" { a.SearchSource = b.SearchSource }
	return a
}

func passes(video model.Video, filters model.SearchFilters, now time.Time) bool {
	if filters.MinViews > 0 && video.Views > 0 && video.Views < filters.MinViews { return false }
	if filters.MinLikes > 0 && video.Likes < filters.MinLikes { return false }
	if filters.MaxDurationSec > 0 && video.DurationSec > filters.MaxDurationSec { return false }
	if filters.PublishedWithinDays > 0 && video.PublishedAt != nil {
		if now.Sub(*video.PublishedAt) > time.Duration(filters.PublishedWithinDays)*24*time.Hour { return false }
	}
	return true
}

func relevanceScore(video model.Video, query string) float64 {
	queryTokens := tokens(query)
	if len(queryTokens) == 0 { return 0.5 }
	haystack := strings.ToLower(video.Title + " " + video.Author + " " + video.SearchSource)
	matched := 0
	for _, token := range queryTokens {
		if strings.Contains(haystack, token) { matched++ }
	}
	score := float64(matched) / float64(len(queryTokens))
	// Results discovered through an expanded keyword inherit semantic relevance from the expander.
	if source := strings.TrimSpace(video.SearchSource); source != "" && !strings.EqualFold(source, strings.TrimSpace(query)) {
		score = math.Max(score, 0.75)
	}
	if strings.Contains(strings.ToLower(video.Title), strings.ToLower(strings.TrimSpace(query))) { score = math.Max(score, 1) }
	return clamp01(score)
}

func affiliateScore(video model.Video) float64 {
	platform := strings.ToLower(strings.TrimSpace(video.Platform))
	sourceFit := map[string]float64{
		"xiaohongshu": 1.00,
		"kuaishou":    0.96,
		"douyin":      0.95,
		"weishi":      0.86,
		"meipai":      0.78,
		"weibo":       0.74,
		"toutiao":     0.72,
		"haokan":      0.64,
		"xigua":       0.60,
		"bilibili":    0.58,
		"acfun":       0.46,
	}[platform]
	if sourceFit == 0 {
		sourceFit = 0.5
	}

	text := strings.ToLower(video.Title + " " + video.SearchSource)
	intentTerms := []string{
		"好物", "种草", "开箱", "测评", "评测", "推荐", "实用", "新品", "爆款", "神器", "必买", "平替", "性价比", "清单",
		"review", "unbox", "đáng mua", "nên mua", "giá tốt", "tiện ích",
	}
	matches := 0
	for _, term := range intentTerms {
		if strings.Contains(text, term) {
			matches++
		}
	}
	intent := math.Min(1, 0.25*float64(matches))
	if matches == 0 {
		intent = 0.35
	}

	durationFit := 0.55
	switch {
	case video.DurationSec <= 0:
		durationFit = 0.55
	case video.DurationSec <= 90:
		durationFit = 1.0
	case video.DurationSec <= 180:
		durationFit = 0.88
	case video.DurationSec <= 300:
		durationFit = 0.65
	default:
		durationFit = 0.40
	}
	return clamp01(0.55*sourceFit + 0.30*intent + 0.15*durationFit)
}

func tokens(value string) []string {
	parts := strings.FieldsFunc(strings.ToLower(value), func(r rune) bool {
		return unicode.IsSpace(r) || unicode.IsPunct(r)
	})
	out := make([]string, 0, len(parts))
	for _, part := range parts { if len([]rune(part)) >= 2 { out = append(out, part) } }
	return out
}

func recencyScore(publishedAt *time.Time, now time.Time) float64 {
	if publishedAt == nil { return 0.35 }
	ageHours := math.Max(0, now.Sub(*publishedAt).Hours())
	return math.Exp(-ageHours / (24 * 14))
}

func publishedUnix(video model.Video) int64 {
	if video.PublishedAt == nil { return 0 }
	return video.PublishedAt.Unix()
}
func normalize(value, max float64) float64 { if max <= 0 { return 0 }; return clamp01(value / max) }
func clamp01(value float64) float64 { return math.Max(0, math.Min(1, value)) }
func round(value float64) float64 { return math.Round(value*10) / 10 }

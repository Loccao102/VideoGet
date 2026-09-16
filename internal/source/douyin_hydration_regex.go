package source

import "regexp"

// Douyin's React hydration chunks are frequently embedded inside JavaScript
// strings, so field quotes can appear as either "field" or \"field\". Keep
// the search parser tolerant of both forms without decoding the entire HTML
// document and risking damage to normal attributes.
func init() {
	douyinAwemeIDPattern = regexp.MustCompile(`(?i)\\?["'](?:aweme_id|awemeId)\\?["']\s*[:=]\s*\\?["']([0-9]{8,})\\?["']`)
	douyinDescPattern = regexp.MustCompile(`(?s)\\?["']desc\\?["']\s*:\s*\\?"([^"\\]*(?:\\.[^"\\]*)*)\\?"`)
	douyinNicknamePattern = regexp.MustCompile(`(?s)\\?["']nickname\\?["']\s*:\s*\\?"([^"\\]*(?:\\.[^"\\]*)*)\\?"`)
	douyinPlayCountPattern = regexp.MustCompile(`(?i)\\?["']play_count\\?["']\s*:\s*([0-9]+)`)
	douyinDiggCountPattern = regexp.MustCompile(`(?i)\\?["']digg_count\\?["']\s*:\s*([0-9]+)`)
	douyinCommentPattern = regexp.MustCompile(`(?i)\\?["']comment_count\\?["']\s*:\s*([0-9]+)`)
	douyinSharePattern = regexp.MustCompile(`(?i)\\?["']share_count\\?["']\s*:\s*([0-9]+)`)
	douyinCreateTimePattern = regexp.MustCompile(`(?i)\\?["']create_time\\?["']\s*:\s*([0-9]+)`)
	douyinDurationPattern = regexp.MustCompile(`(?i)\\?["']duration\\?["']\s*:\s*([0-9]+)`)
}

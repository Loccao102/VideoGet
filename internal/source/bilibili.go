package source

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

// BilibiliProvider uses bilibili-cli for discovery and keeps yt-dlp only for downloads.
// Search requests are deliberately serialized because Bilibili may return HTTP/code -412
// when several search requests are fired concurrently from the same IP/session.
type BilibiliProvider struct {
	binary string
	mu sync.Mutex
	lastSearch time.Time
}

func NewBilibiliProvider() *BilibiliProvider {
	binary := strings.TrimSpace(os.Getenv("BILIBILI_BIN"))
	if binary == "" {
		binary = "bili"
	}
	return &BilibiliProvider{binary: binary}
}

func (p *BilibiliProvider) Name() string { return "bilibili" }
func (p *BilibiliProvider) Available() error { _, err := executable(p.binary); return err }

func (p *BilibiliProvider) Search(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	bin, err := executable(p.binary)
	if err != nil {
		return nil, err
	}

	if limit <= 0 {
		limit = 20
	}

	// One Bilibili search at a time. Keyword expansion can otherwise launch 5-8
	// simultaneous searches and trigger Bilibili's risk-control response (-412).
	p.mu.Lock()
	defer p.mu.Unlock()

	delay := envDurationMS("BILIBILI_SEARCH_DELAY_MS", 1200)
	if wait := delay - time.Since(p.lastSearch); !p.lastSearch.IsZero() && wait > 0 {
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
	p.lastSearch = time.Now()

	args := []string{
		"search", keyword,
		"--type", "video",
		"--limit", strconv.Itoa(limit),
		"--output", "jsonl",
		"--quiet",
		"--color", "never",
		"--rate", envString("BILIBILI_REQUEST_RATE", "800ms"),
		"--retries", envString("BILIBILI_RETRIES", "2"),
	}
	if cookie := strings.TrimSpace(os.Getenv("BILIBILI_COOKIE")); cookie != "" {
		args = append(args, "--cookie", cookie)
	}

	cmd := exec.CommandContext(ctx, bin, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("create bili stdout pipe: %w", err)
	}
	var stderr strings.Builder
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start bilibili-cli: %w", err)
	}

	results := make([]model.Video, 0, limit)
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		var item struct {
			BVID string `json:"bvid"`
			Title string `json:"title"`
			Description string `json:"description"`
			OwnerName string `json:"owner_name"`
			Duration int64 `json:"duration_seconds"`
			ViewCount int64 `json:"view_count"`
			ReplyCount int64 `json:"reply_count"`
			ShareCount int64 `json:"share_count"`
			LikeCount int64 `json:"like_count"`
			Pubdate int64 `json:"pubdate"`
			CoverURL string `json:"cover_url"`
			URL string `json:"url"`
		}
		if err := json.Unmarshal(scanner.Bytes(), &item); err != nil {
			continue
		}
		if strings.TrimSpace(item.BVID) == "" {
			continue
		}
		videoURL := strings.TrimSpace(item.URL)
		if videoURL == "" {
			videoURL = "https://www.bilibili.com/video/" + item.BVID
		}
		video := model.Video{
			ID: item.BVID,
			Platform: p.Name(),
			Title: item.Title,
			Author: item.OwnerName,
			URL: videoURL,
			Thumbnail: item.CoverURL,
			DurationSec: item.Duration,
			Views: item.ViewCount,
			Likes: item.LikeCount,
			Comments: item.ReplyCount,
			Shares: item.ShareCount,
			SearchSource: keyword,
		}
		if item.Pubdate > 0 {
			t := time.Unix(item.Pubdate, 0).UTC()
			video.PublishedAt = &t
		}
		results = append(results, video)
		if len(results) >= limit {
			break
		}
	}
	if err := scanner.Err(); err != nil {
		_ = cmd.Wait()
		return nil, fmt.Errorf("read bilibili search output: %w", err)
	}

	if err := cmd.Wait(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		lower := strings.ToLower(message)
		if strings.Contains(lower, "412") || strings.Contains(lower, "risk") || strings.Contains(message, "风控") {
			return nil, fmt.Errorf("bilibili temporarily refused the search request (risk control / 412). Reduce search frequency, wait before retrying, or provide BILIBILI_COOKIE from your own logged-in session: %s", message)
		}
		return nil, fmt.Errorf("bilibili search failed: %s", message)
	}
	return results, nil
}

func envDurationMS(key string, fallback int) time.Duration {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return time.Duration(fallback) * time.Millisecond
	}
	milliseconds, err := strconv.Atoi(value)
	if err != nil || milliseconds < 0 {
		return time.Duration(fallback) * time.Millisecond
	}
	return time.Duration(milliseconds) * time.Millisecond
}

func envString(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

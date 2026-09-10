package source

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type BilibiliProvider struct{ binary string }

func NewBilibiliProvider() *BilibiliProvider { return &BilibiliProvider{binary: "yt-dlp"} }
func (p *BilibiliProvider) Name() string       { return "bilibili" }
func (p *BilibiliProvider) Available() error  { _, err := executable(p.binary); return err }

func (p *BilibiliProvider) Search(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	bin, err := executable(p.binary)
	if err != nil {
		return nil, err
	}
	cmd := exec.CommandContext(ctx, bin, "--ignore-config", "--flat-playlist", "--dump-json", "--no-warnings", "--playlist-end", strconv.Itoa(limit), "bilisearch:"+keyword)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("create yt-dlp stdout pipe: %w", err)
	}
	var stderr strings.Builder
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start yt-dlp: %w", err)
	}

	results := make([]model.Video, 0, limit)
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		var item struct {
			ID           string   `json:"id"`
			Title        string   `json:"title"`
			URL          string   `json:"url"`
			WebpageURL   string   `json:"webpage_url"`
			OriginalURL  string   `json:"original_url"`
			Thumbnail    string   `json:"thumbnail"`
			Uploader     string   `json:"uploader"`
			Channel      string   `json:"channel"`
			Duration     *float64 `json:"duration"`
			Timestamp    *int64   `json:"timestamp"`
			ViewCount    *int64   `json:"view_count"`
			LikeCount    *int64   `json:"like_count"`
			CommentCount *int64   `json:"comment_count"`
		}
		if err := json.Unmarshal(scanner.Bytes(), &item); err != nil {
			continue
		}
		videoURL := firstNonEmpty(item.WebpageURL, item.OriginalURL, item.URL)
		if videoURL == "" && item.ID != "" {
			videoURL = "https://www.bilibili.com/video/" + item.ID
		}
		v := model.Video{ID: item.ID, Platform: p.Name(), Title: item.Title, Author: firstNonEmpty(item.Uploader, item.Channel), URL: videoURL, Thumbnail: item.Thumbnail, SearchSource: keyword}
		if item.Duration != nil {
			v.DurationSec = int64(*item.Duration)
		}
		if item.ViewCount != nil {
			v.Views = *item.ViewCount
		}
		if item.LikeCount != nil {
			v.Likes = *item.LikeCount
		}
		if item.CommentCount != nil {
			v.Comments = *item.CommentCount
		}
		if item.Timestamp != nil && *item.Timestamp > 0 {
			t := time.Unix(*item.Timestamp, 0).UTC()
			v.PublishedAt = &t
		}
		results = append(results, v)
	}
	if err := scanner.Err(); err != nil {
		_ = cmd.Wait()
		return nil, err
	}
	if err := cmd.Wait(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return nil, fmt.Errorf("bilibili search failed: %s", msg)
	}
	return results, nil
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

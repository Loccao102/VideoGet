package download

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// douyinResumableDirect keeps the current CLI-free/yt-dlp-free Douyin resolver
// but hardens the media transfer. Douyin CDN connections can close early; this
// path retains the .part file and resumes with HTTP Range instead of restarting.
func (m *Manager) douyinResumableDirect(ctx context.Context, rawURL, outputDir string) (string, error) {
	videoID, err := resolveDouyinVideoID(ctx, rawURL)
	if err != nil {
		return "", err
	}

	shareURL := "https://www.iesdouyin.com/share/video/" + videoID + "/"
	document, err := fetchDouyinSharePage(ctx, shareURL)
	if err != nil {
		return "", fmt.Errorf("load Douyin share page: %w", err)
	}

	candidates, parseErr := douyinRouterDataCandidates(document)
	for _, candidate := range douyinMediaCandidates(document) {
		candidates = appendUniqueDouyinURL(candidates, candidate)
	}
	if len(candidates) == 0 {
		if parseErr != nil {
			return "", fmt.Errorf("Douyin _ROUTER_DATA parse failed: %w", parseErr)
		}
		return "", fmt.Errorf("Douyin share page contained no playable video URL")
	}

	cookie := strings.TrimSpace(os.Getenv("DOUYIN_COOKIE"))
	var failures []string
	maxCandidates := envPositiveInt("DOUYIN_MEDIA_CANDIDATES", 12)
	if maxCandidates > len(candidates) {
		maxCandidates = len(candidates)
	}
	for index, mediaURL := range candidates[:maxCandidates] {
		output := filepath.Join(outputDir, fmt.Sprintf("douyin-%s-%02d.mp4", videoID, index+1))
		if err := downloadDouyinMediaResumable(ctx, mediaURL, shareURL, output, cookie); err != nil {
			failures = append(failures, fmt.Sprintf("candidate %d: %v", index+1, err))
			continue
		}
		if !hasAudioStream(output) {
			_ = os.Remove(output)
			failures = append(failures, fmt.Sprintf("candidate %d: downloaded media has no audio", index+1))
			continue
		}
		return output, nil
	}

	if len(failures) == 0 {
		return "", fmt.Errorf("Douyin download returned no usable media")
	}
	return "", fmt.Errorf("Douyin resumable download failed: %s", strings.Join(failures, " | "))
}

func downloadDouyinMediaResumable(ctx context.Context, rawURL, referer, output, cookie string) error {
	partial := output + ".part"
	maxAttempts := envPositiveInt("DOUYIN_MEDIA_RESUME_ATTEMPTS", 40)
	stallLimit := envPositiveInt("DOUYIN_MEDIA_STALL_LIMIT", 5)
	client := &http.Client{Timeout: time.Duration(envPositiveInt("DOUYIN_MEDIA_TIMEOUT_SEC", 120)) * time.Second}

	var expectedTotal int64 = -1
	var lastErr error
	stalls := 0

	for attempt := 1; attempt <= maxAttempts; attempt++ {
		if err := ctx.Err(); err != nil {
			return err
		}

		offset := douyinPartialSize(partial)
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
		if err != nil {
			return err
		}
		setDouyinMediaHeaders(req, referer, cookie)
		if offset > 0 {
			req.Header.Set("Range", fmt.Sprintf("bytes=%d-", offset))
		}

		resp, err := client.Do(req)
		if err != nil {
			lastErr = err
			stalls++
			if stalls >= stallLimit {
				return fmt.Errorf("Douyin CDN stalled after %d attempts at byte %d: %w", attempt, offset, err)
			}
			continue
		}

		if resp.StatusCode == http.StatusRequestedRangeNotSatisfiable {
			total := douyinUnsatisfiedRangeTotal(resp.Header.Get("Content-Range"))
			_ = resp.Body.Close()
			if total > 0 && offset >= total {
				return finalizeDouyinPartial(partial, output)
			}
			return fmt.Errorf("Douyin CDN rejected resume range at byte %d (HTTP 416)", offset)
		}
		if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
			status := resp.StatusCode
			_ = resp.Body.Close()
			if status >= 400 && status < 500 && status != http.StatusTooManyRequests {
				return fmt.Errorf("HTTP %d", status)
			}
			lastErr = fmt.Errorf("HTTP %d", status)
			continue
		}

		contentType := strings.ToLower(resp.Header.Get("Content-Type"))
		if strings.Contains(contentType, "text/html") || strings.Contains(contentType, "application/json") {
			_ = resp.Body.Close()
			return fmt.Errorf("unexpected content-type %q", contentType)
		}

		appendMode := offset > 0 && resp.StatusCode == http.StatusPartialContent
		if resp.StatusCode == http.StatusPartialContent {
			start, total, ok := parseDouyinContentRange(resp.Header.Get("Content-Range"))
			if !ok {
				_ = resp.Body.Close()
				return fmt.Errorf("invalid Content-Range %q", resp.Header.Get("Content-Range"))
			}
			if start != offset {
				_ = resp.Body.Close()
				return fmt.Errorf("resume range mismatch: requested %d, server started at %d", offset, start)
			}
			if total > 0 {
				expectedTotal = total
			}
		} else {
			// Some CDNs ignore Range. In that case restart safely from byte 0
			// using this 200 response instead of appending duplicate bytes.
			appendMode = false
			offset = 0
			if resp.ContentLength > 0 {
				expectedTotal = resp.ContentLength
			}
		}

		flags := os.O_CREATE | os.O_WRONLY
		if appendMode {
			flags |= os.O_APPEND
		} else {
			flags |= os.O_TRUNC
		}
		file, err := os.OpenFile(partial, flags, 0o644)
		if err != nil {
			_ = resp.Body.Close()
			return err
		}
		_, copyErr := io.Copy(file, resp.Body)
		bodyCloseErr := resp.Body.Close()
		fileCloseErr := file.Close()
		if fileCloseErr != nil {
			return fileCloseErr
		}
		if bodyCloseErr != nil && copyErr == nil {
			copyErr = bodyCloseErr
		}

		newSize := douyinPartialSize(partial)
		if expectedTotal > 0 && newSize >= expectedTotal {
			return finalizeDouyinPartial(partial, output)
		}
		if copyErr == nil && expectedTotal <= 0 {
			return finalizeDouyinPartial(partial, output)
		}

		if copyErr != nil {
			lastErr = copyErr
		} else if expectedTotal > 0 {
			lastErr = fmt.Errorf("short Douyin CDN response: got %d of %d bytes", newSize, expectedTotal)
		} else {
			lastErr = fmt.Errorf("Douyin CDN response ended before completion")
		}

		if newSize <= offset {
			stalls++
		} else {
			stalls = 0
		}
		if stalls >= stallLimit {
			return fmt.Errorf("Douyin CDN made no progress for %d attempts at byte %d: %w", stalls, newSize, lastErr)
		}
	}

	if lastErr == nil {
		lastErr = fmt.Errorf("download did not complete")
	}
	return fmt.Errorf("Douyin CDN resume attempts exhausted (%d): %w", maxAttempts, lastErr)
}

func setDouyinMediaHeaders(req *http.Request, referer, cookie string) {
	req.Header.Set("User-Agent", douyinMobileUserAgent())
	req.Header.Set("Accept", "video/*,application/octet-stream;q=0.9,*/*;q=0.5")
	if referer != "" {
		req.Header.Set("Referer", referer)
	}
	if cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
}

func douyinPartialSize(path string) int64 {
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return 0
	}
	return info.Size()
}

func finalizeDouyinPartial(partial, output string) error {
	info, err := os.Stat(partial)
	if err != nil {
		return err
	}
	if info.Size() < 64*1024 {
		return fmt.Errorf("downloaded payload is too small to be a video")
	}
	_ = os.Remove(output)
	if err := os.Rename(partial, output); err != nil {
		return err
	}
	return nil
}

func parseDouyinContentRange(value string) (start int64, total int64, ok bool) {
	fields := strings.Fields(strings.TrimSpace(value))
	if len(fields) != 2 || !strings.EqualFold(fields[0], "bytes") {
		return 0, 0, false
	}
	parts := strings.SplitN(fields[1], "/", 2)
	if len(parts) != 2 || parts[0] == "*" || parts[1] == "*" {
		return 0, 0, false
	}
	bounds := strings.SplitN(parts[0], "-", 2)
	if len(bounds) != 2 {
		return 0, 0, false
	}
	start, err := strconv.ParseInt(bounds[0], 10, 64)
	if err != nil || start < 0 {
		return 0, 0, false
	}
	end, err := strconv.ParseInt(bounds[1], 10, 64)
	if err != nil || end < start {
		return 0, 0, false
	}
	total, err = strconv.ParseInt(parts[1], 10, 64)
	if err != nil || total <= end {
		return 0, 0, false
	}
	return start, total, true
}

func douyinUnsatisfiedRangeTotal(value string) int64 {
	fields := strings.Fields(strings.TrimSpace(value))
	if len(fields) != 2 || !strings.EqualFold(fields[0], "bytes") {
		return 0
	}
	parts := strings.SplitN(fields[1], "/", 2)
	if len(parts) != 2 || parts[0] != "*" {
		return 0
	}
	total, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil || total <= 0 {
		return 0
	}
	return total
}

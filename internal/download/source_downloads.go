package download

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

func (m *Manager) ytdlpBilibili(ctx context.Context, rawURL, outputDir string) (string, error) {
	bin, err := exec.LookPath("yt-dlp")
	if err != nil {
		return "", fmt.Errorf("yt-dlp is not installed: %w", err)
	}

	template := filepath.Join(outputDir, "%(title).120B [%(id)s].%(ext)s")
	formats := []string{
		"bv*[height<=1080]+ba/b[height<=1080]/b",
		"bv*[height<=720]+ba/b[height<=720]/b",
		"bv*[height<=480]+ba/b[height<=480]/b",
		"bv*+ba/b",
	}
	attempts := envPositiveInt("BILIBILI_DOWNLOAD_ATTEMPTS", len(formats))
	if attempts > len(formats) {
		attempts = len(formats)
	}
	cookie := strings.TrimSpace(os.Getenv("BILIBILI_COOKIE"))
	userAgent := strings.TrimSpace(os.Getenv("BILIBILI_USER_AGENT"))
	if userAgent == "" {
		userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
	}

	var errors []string
	for attempt := 0; attempt < attempts; attempt++ {
		cleanupPartialFiles(outputDir)
		args := []string{
			"--ignore-config",
			"--no-playlist",
			"--no-progress",
			"--no-continue",
			"--retries", "3",
			"--fragment-retries", "3",
			"--retry-sleep", "3",
			"--socket-timeout", "25",
			"--concurrent-fragments", "1",
			"--merge-output-format", "mp4",
			"--format", formats[attempt],
			"--referer", "https://www.bilibili.com/",
			"--user-agent", userAgent,
			"--add-header", "Origin:https://www.bilibili.com",
			"--print", "after_move:filepath",
			"-o", template,
		}
		if cookie != "" {
			args = append(args, "--add-header", "Cookie:"+cookie)
		}
		args = append(args, rawURL)

		cmd := exec.CommandContext(ctx, bin, args...)
		var stdout, stderr bytes.Buffer
		cmd.Stdout = &stdout
		cmd.Stderr = &stderr
		err := cmd.Run()
		if err == nil {
			candidate := resolveYTDLPOutput(stdout.String())
			if candidate != "" {
				if hasAudioStream(candidate) {
					return candidate, nil
				}
				_ = os.Remove(candidate)
				errors = append(errors, fmt.Sprintf("attempt %d downloaded media without audio stream", attempt+1))
			} else {
				errors = append(errors, fmt.Sprintf("attempt %d finished but output file could not be resolved", attempt+1))
			}
		} else {
			message := strings.TrimSpace(stderr.String())
			if message == "" {
				message = err.Error()
			}
			if len(message) > 900 {
				message = message[:900]
			}
			errors = append(errors, fmt.Sprintf("attempt %d format=%q: %s", attempt+1, formats[attempt], message))
		}

		if attempt+1 < attempts {
			wait := time.Duration(3*(1<<attempt)) * time.Second
			select {
			case <-time.After(wait):
			case <-ctx.Done():
				return "", ctx.Err()
			}
		}
	}

	// yt-dlp's Bilibili extractor can get a perfectly valid play response but then
	// hit a transient 5xx on the selected CDN URL. bili-cli exposes Bilibili's
	// backup_urls, so use those as a final anonymous/public fallback.
	if envDownloadBool("BILIBILI_STREAM_FALLBACK", true) {
		if candidate, fallbackErr := m.biliStreamsFallback(ctx, rawURL, outputDir, cookie, userAgent); fallbackErr == nil {
			return candidate, nil
		} else {
			errors = append(errors, "bili-stream-fallback: "+fallbackErr.Error())
		}
	}
	return "", fmt.Errorf("Bilibili download failed after %d yt-dlp strategies: %s", attempts, strings.Join(errors, " | "))
}

type biliPlayableStream struct {
	Quality     int      `json:"quality"`
	QualityText string   `json:"quality_text"`
	MIME        string   `json:"mime"`
	Bandwidth   int64    `json:"bandwidth"`
	Width       int      `json:"width"`
	Height      int      `json:"height"`
	URL         string   `json:"url"`
	BackupURLs  []string `json:"backup_urls"`
}

func (m *Manager) biliStreamsFallback(ctx context.Context, rawURL, outputDir, cookie, userAgent string) (string, error) {
	bin, err := exec.LookPath("bili")
	if err != nil {
		return "", fmt.Errorf("bili CLI is not installed: %w", err)
	}
	args := []string{
		"streams", rawURL,
		"--quality", "80",
		"-o", "jsonl",
		"--quiet",
		"--no-cache",
		"--retries", "4",
		"--timeout", "30s",
		"--rate", "600ms",
	}
	if cookie != "" {
		args = append(args, "--cookie", cookie)
	}
	cmd := exec.CommandContext(ctx, bin, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return "", fmt.Errorf("resolve streams: %s", message)
	}

	var streams []biliPlayableStream
	scanner := bufio.NewScanner(bytes.NewReader(stdout.Bytes()))
	buffer := make([]byte, 64*1024)
	scanner.Buffer(buffer, 2<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var stream biliPlayableStream
		if err := json.Unmarshal([]byte(line), &stream); err != nil {
			continue
		}
		if strings.TrimSpace(stream.URL) != "" {
			streams = append(streams, stream)
		}
	}
	if err := scanner.Err(); err != nil {
		return "", fmt.Errorf("read streams: %w", err)
	}
	if len(streams) == 0 {
		return "", fmt.Errorf("bili streams returned no playable URLs")
	}

	videoIndex, audioIndex, combinedIndex := selectBiliStreams(streams)
	headers := map[string]string{
		"Referer":    "https://www.bilibili.com/",
		"Origin":     "https://www.bilibili.com",
		"User-Agent": userAgent,
	}
	if cookie != "" {
		headers["Cookie"] = cookie
	}

	if videoIndex < 0 {
		if combinedIndex < 0 {
			return "", fmt.Errorf("stream list contains neither DASH video nor combined media")
		}
		combinedPath := filepath.Join(outputDir, "bilibili-fallback.flv")
		if err := downloadBiliStream(ctx, candidateStreamURLs(streams[combinedIndex]), combinedPath, headers); err != nil {
			return "", fmt.Errorf("download combined stream: %w", err)
		}
		if !hasAudioStream(combinedPath) {
			_ = os.Remove(combinedPath)
			return "", fmt.Errorf("combined fallback stream has no audio")
		}
		return combinedPath, nil
	}
	if audioIndex < 0 {
		return "", fmt.Errorf("DASH video was found but no audio stream was returned")
	}

	videoPath := filepath.Join(outputDir, "bilibili-fallback-video.m4s")
	audioPath := filepath.Join(outputDir, "bilibili-fallback-audio.m4s")
	defer os.Remove(videoPath)
	defer os.Remove(audioPath)
	if err := downloadBiliStream(ctx, candidateStreamURLs(streams[videoIndex]), videoPath, headers); err != nil {
		return "", fmt.Errorf("download video stream: %w", err)
	}
	if err := downloadBiliStream(ctx, candidateStreamURLs(streams[audioIndex]), audioPath, headers); err != nil {
		return "", fmt.Errorf("download audio stream: %w", err)
	}

	ffmpeg, err := exec.LookPath("ffmpeg")
	if err != nil {
		return "", fmt.Errorf("ffmpeg is not installed: %w", err)
	}
	output := filepath.Join(outputDir, "bilibili-fallback.mkv")
	cmd = exec.CommandContext(ctx, ffmpeg,
		"-y", "-loglevel", "error",
		"-i", videoPath,
		"-i", audioPath,
		"-map", "0:v:0",
		"-map", "1:a:0",
		"-c", "copy",
		output,
	)
	if out, err := cmd.CombinedOutput(); err != nil {
		_ = os.Remove(output)
		return "", fmt.Errorf("merge backup streams: %s", strings.TrimSpace(string(out)))
	}
	if !hasAudioStream(output) {
		_ = os.Remove(output)
		return "", fmt.Errorf("merged backup media has no audio")
	}
	return output, nil
}

func selectBiliStreams(streams []biliPlayableStream) (videoIndex, audioIndex, combinedIndex int) {
	videoIndex, audioIndex, combinedIndex = -1, -1, -1
	bestVideoHeight := -1
	bestVideoBandwidth := int64(-1)
	bestAudioBandwidth := int64(-1)
	for i, stream := range streams {
		mime := strings.ToLower(strings.TrimSpace(stream.MIME))
		switch {
		case strings.HasPrefix(mime, "audio/"):
			if stream.Bandwidth > bestAudioBandwidth {
				audioIndex = i
				bestAudioBandwidth = stream.Bandwidth
			}
		case strings.HasPrefix(mime, "video/") && (stream.Width > 0 || stream.Height > 0):
			// Prefer the highest stream up to 1080p. Anonymous Bilibili often tops out
			// at 480/720p, which is perfectly adequate for affiliate shorts.
			height := stream.Height
			if height <= 1080 && (height > bestVideoHeight || (height == bestVideoHeight && stream.Bandwidth > bestVideoBandwidth)) {
				videoIndex = i
				bestVideoHeight = height
				bestVideoBandwidth = stream.Bandwidth
			}
		case strings.HasPrefix(mime, "video/"):
			if combinedIndex < 0 || stream.Bandwidth > streams[combinedIndex].Bandwidth {
				combinedIndex = i
			}
		}
	}
	if videoIndex < 0 {
		// If all returned DASH videos happen to be above 1080p, still choose the
		// smallest one rather than declaring the response unusable.
		for i, stream := range streams {
			mime := strings.ToLower(strings.TrimSpace(stream.MIME))
			if !strings.HasPrefix(mime, "video/") || stream.Height <= 0 {
				continue
			}
			if videoIndex < 0 || stream.Height < streams[videoIndex].Height {
				videoIndex = i
			}
		}
	}
	return videoIndex, audioIndex, combinedIndex
}

func candidateStreamURLs(stream biliPlayableStream) []string {
	out := make([]string, 0, 1+len(stream.BackupURLs))
	seen := map[string]struct{}{}
	for _, candidate := range append([]string{stream.URL}, stream.BackupURLs...) {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		if _, ok := seen[candidate]; ok {
			continue
		}
		seen[candidate] = struct{}{}
		out = append(out, candidate)
	}
	return out
}

func downloadBiliStream(ctx context.Context, urls []string, destination string, headers map[string]string) error {
	if len(urls) == 0 {
		return fmt.Errorf("no CDN URL")
	}
	client := &http.Client{Timeout: 90 * time.Second}
	var failures []string
	for urlIndex, rawURL := range urls {
		for attempt := 0; attempt < 2; attempt++ {
			req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
			if err != nil {
				return err
			}
			for key, value := range headers {
				if strings.TrimSpace(value) != "" {
					req.Header.Set(key, value)
				}
			}
			resp, err := client.Do(req)
			if err == nil && resp.StatusCode >= 200 && resp.StatusCode < 300 {
				file, createErr := os.Create(destination)
				if createErr != nil {
					resp.Body.Close()
					return createErr
				}
				_, copyErr := io.Copy(file, resp.Body)
				closeErr := file.Close()
				resp.Body.Close()
				if copyErr == nil && closeErr == nil {
					if info, statErr := os.Stat(destination); statErr == nil && info.Size() > 0 {
						return nil
					}
				}
				_ = os.Remove(destination)
				failures = append(failures, fmt.Sprintf("cdn %d attempt %d: interrupted transfer", urlIndex+1, attempt+1))
			} else {
				status := "network error"
				if resp != nil {
					status = resp.Status
					resp.Body.Close()
				} else if err != nil {
					status = err.Error()
				}
				failures = append(failures, fmt.Sprintf("cdn %d attempt %d: %s", urlIndex+1, attempt+1, status))
			}
			if ctx.Err() != nil {
				return ctx.Err()
			}
			select {
			case <-time.After(time.Duration(2+attempt*2) * time.Second):
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
	return fmt.Errorf("all CDN URLs failed: %s", strings.Join(failures, "; "))
}

func envDownloadBool(name string, fallback bool) bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv(name)))
	if value == "" {
		return fallback
	}
	return value != "0" && value != "false" && value != "no" && value != "off"
}

func resolveYTDLPOutput(stdout string) string {
	lines := strings.Split(strings.TrimSpace(stdout), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		candidate := strings.TrimSpace(lines[i])
		if candidate == "" {
			continue
		}
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate
		}
	}
	return ""
}

func hasAudioStream(path string) bool {
	bin, err := exec.LookPath("ffprobe")
	if err != nil {
		return true
	}
	cmd := exec.Command(bin,
		"-v", "error",
		"-select_streams", "a:0",
		"-show_entries", "stream=index",
		"-of", "csv=p=0",
		path,
	)
	out, err := cmd.Output()
	return err == nil && strings.TrimSpace(string(out)) != ""
}

func cleanupPartialFiles(root string) {
	_ = filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil || entry.IsDir() {
			return nil
		}
		lower := strings.ToLower(entry.Name())
		if strings.HasSuffix(lower, ".part") || strings.Contains(lower, ".part-") || strings.HasSuffix(lower, ".ytdl") {
			_ = os.Remove(path)
		}
		return nil
	})
}

func (m *Manager) douyin(ctx context.Context, rawURL, outputDir string) (string, error) {
	binary := strings.TrimSpace(os.Getenv("DOUYIN_BIN"))
	if binary == "" {
		binary = "douyin"
	}
	bin, err := exec.LookPath(binary)
	if err != nil {
		return "", fmt.Errorf("douyin-cli is not installed: %w", err)
	}

	startedAt := time.Now().Add(-2 * time.Second)
	cmd := exec.CommandContext(ctx, bin, "-u", rawURL, "-t", "aweme", "-p", outputDir)
	cmd.Env = os.Environ()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("douyin download failed: %s", strings.TrimSpace(string(out)))
	}

	output, err := newestMediaFile(outputDir, startedAt)
	if err != nil {
		return "", fmt.Errorf("douyin download finished but output video was not found: %w", err)
	}
	if !hasAudioStream(output) {
		return "", fmt.Errorf("douyin downloaded media without an audio stream: %s", output)
	}
	return output, nil
}

func newestMediaFile(root string, after time.Time) (string, error) {
	type candidate struct {
		path string
		mod  time.Time
	}
	var files []candidate
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		ext := strings.ToLower(filepath.Ext(path))
		switch ext {
		case ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv":
		default:
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return nil
		}
		if info.ModTime().Before(after) {
			return nil
		}
		files = append(files, candidate{path: path, mod: info.ModTime()})
		return nil
	})
	if err != nil {
		return "", err
	}
	if len(files) == 0 {
		return "", fmt.Errorf("no newly-created media file in %s", root)
	}
	sort.Slice(files, func(i, j int) bool { return files[i].mod.After(files[j].mod) })
	return files[0].path, nil
}

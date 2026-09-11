package download

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

func (m *Manager) ytdlpPublic(ctx context.Context, platform, rawURL, outputDir string) (string, error) {
	bin, err := exec.LookPath("yt-dlp")
	if err != nil {
		return "", fmt.Errorf("yt-dlp is not installed: %w", err)
	}

	template := filepath.Join(outputDir, "%(title).120B [%(id)s].%(ext)s")
	formats := []string{
		"bv*[height<=1080]+ba/b[height<=1080]/b",
		"bv*+ba/b",
		"b[ext=mp4]/b",
	}
	attempts := envPositiveInt("PUBLIC_DOWNLOAD_ATTEMPTS", 2)
	if attempts > len(formats) {
		attempts = len(formats)
	}
	userAgent := strings.TrimSpace(os.Getenv("PUBLIC_DOWNLOAD_USER_AGENT"))
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
			"--retry-sleep", "2",
			"--socket-timeout", "25",
			"--concurrent-fragments", "1",
			"--merge-output-format", "mp4",
			"--format", formats[attempt],
			"--user-agent", userAgent,
			"--print", "after_move:filepath",
			"-o", template,
			rawURL,
		}
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
			// Changing the requested format cannot turn an image-only post into a video.
			// Stop immediately so one bad Xiaohongshu candidate does not waste two long attempts.
			if strings.EqualFold(platform, "xiaohongshu") && strings.Contains(strings.ToLower(message), "no video formats found") {
				return "", fmt.Errorf("Xiaohongshu note không có video format công khai (thường là bài ảnh hoặc note cần xsec/session). Hãy chọn candidate được preview xác nhận là Video")
			}
			errors = append(errors, fmt.Sprintf("attempt %d: %s", attempt+1, message))
		}

		if attempt+1 < attempts {
			select {
			case <-time.After(time.Duration(2*(attempt+1)) * time.Second):
			case <-ctx.Done():
				return "", ctx.Err()
			}
		}
	}
	return "", fmt.Errorf("%s public download failed after %d strategies: %s", platform, attempts, strings.Join(errors, " | "))
}

func isPublicDownloadPlatform(platform string) bool {
	switch strings.ToLower(strings.TrimSpace(platform)) {
	case "kuaishou", "xiaohongshu", "weibo", "xigua", "haokan", "toutiao", "acfun", "meipai", "weishi":
		return true
	default:
		return false
	}
}

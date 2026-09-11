package download

import (
	"bytes"
	"context"
	"fmt"
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
		"bv*+ba/b",
		"b[ext=mp4]/b",
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
	return "", fmt.Errorf("yt-dlp Bilibili download failed after %d strategies: %s", attempts, strings.Join(errors, " | "))
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
		case ".mp4", ".mkv", ".webm", ".mov", ".m4v":
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

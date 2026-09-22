package download

import (
	"context"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var (
	kuaishouInitStatePattern   = regexp.MustCompile(`(?s)window\.INIT_STATE\s*=\s*(\{.*?\})\s*;?\s*</script>`)
	kuaishouApolloStatePattern = regexp.MustCompile(`(?s)window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>`)
	kuaishouPhotoURLPattern    = regexp.MustCompile(`(?s)["']photoUrl["']\s*:\s*["']((?:\\.|[^"'\\])+)["']`)
	kuaishouMainMVPattern      = regexp.MustCompile(`(?s)["']mainMvUrls["']\s*:\s*\[(.*?)\]`)
	kuaishouURLPattern         = regexp.MustCompile(`(?s)["']url["']\s*:\s*["']((?:\\.|[^"'\\])+)["']`)
)

func (m *Manager) kuaishou(ctx context.Context, rawURL, outputDir string) (string, error) {
	direct, directErr := downloadKuaishouFromPage(ctx, rawURL, outputDir)
	if directErr == nil {
		return direct, nil
	}

	// Keep the generic extractor as a last compatibility fallback. yt-dlp does
	// not currently advertise a dedicated Kuaishou extractor, but its generic
	// extractor can still work for some share-page variants.
	generic, genericErr := m.ytdlpPublic(ctx, "kuaishou", rawURL, outputDir)
	if genericErr == nil {
		return generic, nil
	}
	return "", fmt.Errorf("kuaishou download failed: page-state=%v | generic=%v", directErr, genericErr)
}

func downloadKuaishouFromPage(ctx context.Context, rawURL, outputDir string) (string, error) {
	client := &http.Client{Timeout: envDurationSeconds("KUAISHOU_PAGE_TIMEOUT_SEC", 35)}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return "", err
	}
	ua := strings.TrimSpace(os.Getenv("PUBLIC_DOWNLOAD_USER_AGENT"))
	if ua == "" {
		ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
	}
	req.Header.Set("User-Agent", ua)
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.7")

	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("open Kuaishou page: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("Kuaishou page HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 24<<20))
	if err != nil {
		return "", fmt.Errorf("read Kuaishou page: %w", err)
	}
	mediaURL := extractKuaishouMediaURL(body)
	if mediaURL == "" {
		return "", fmt.Errorf("Kuaishou page contained no public video URL in INIT_STATE/APOLLO_STATE")
	}

	mediaReq, err := http.NewRequestWithContext(ctx, http.MethodGet, mediaURL, nil)
	if err != nil {
		return "", err
	}
	mediaReq.Header.Set("User-Agent", ua)
	mediaReq.Header.Set("Referer", resp.Request.URL.String())
	mediaResp, err := (&http.Client{Timeout: envDurationSeconds("KUAISHOU_MEDIA_TIMEOUT_SEC", 120)}).Do(mediaReq)
	if err != nil {
		return "", fmt.Errorf("download Kuaishou media: %w", err)
	}
	defer mediaResp.Body.Close()
	if mediaResp.StatusCode < 200 || mediaResp.StatusCode >= 300 {
		return "", fmt.Errorf("Kuaishou media HTTP %d", mediaResp.StatusCode)
	}

	out := filepath.Join(outputDir, "kuaishou-source.mp4")
	f, err := os.Create(out)
	if err != nil {
		return "", err
	}
	_, copyErr := io.Copy(f, mediaResp.Body)
	closeErr := f.Close()
	if copyErr != nil {
		_ = os.Remove(out)
		return "", fmt.Errorf("save Kuaishou media: %w", copyErr)
	}
	if closeErr != nil {
		_ = os.Remove(out)
		return "", closeErr
	}
	if info, err := os.Stat(out); err != nil || info.Size() == 0 {
		_ = os.Remove(out)
		return "", fmt.Errorf("Kuaishou media download was empty")
	}
	return out, nil
}

func extractKuaishouMediaURL(body []byte) string {
	text := html.UnescapeString(string(body))

	for _, pattern := range []*regexp.Regexp{kuaishouInitStatePattern, kuaishouApolloStatePattern} {
		match := pattern.FindStringSubmatch(text)
		if len(match) != 2 {
			continue
		}
		var root any
		if json.Unmarshal([]byte(match[1]), &root) == nil {
			if u := findKuaishouVideoURL(root); u != "" {
				return u
			}
		}
	}

	// State payloads occasionally contain JS values that prevent strict JSON
	// decoding. Fall back to the stable video fields without depending on the
	// surrounding object shape.
	if match := kuaishouPhotoURLPattern.FindStringSubmatch(text); len(match) == 2 {
		if u := decodeKuaishouJSONString(match[1]); strings.HasPrefix(u, "http") {
			return u
		}
	}
	if block := kuaishouMainMVPattern.FindStringSubmatch(text); len(block) == 2 {
		if match := kuaishouURLPattern.FindStringSubmatch(block[1]); len(match) == 2 {
			if u := decodeKuaishouJSONString(match[1]); strings.HasPrefix(u, "http") {
				return u
			}
		}
	}
	return ""
}

func findKuaishouVideoURL(value any) string {
	switch node := value.(type) {
	case map[string]any:
		for key, child := range node {
			switch strings.ToLower(key) {
			case "photourl":
				if u, ok := child.(string); ok && strings.HasPrefix(u, "http") {
					return u
				}
			case "mainmvurls":
				if u := firstURLFromKuaishouList(child); u != "" {
					return u
				}
			}
		}
		for _, child := range node {
			if u := findKuaishouVideoURL(child); u != "" {
				return u
			}
		}
	case []any:
		for _, child := range node {
			if u := findKuaishouVideoURL(child); u != "" {
				return u
			}
		}
	}
	return ""
}

func firstURLFromKuaishouList(value any) string {
	items, ok := value.([]any)
	if !ok {
		return ""
	}
	for _, item := range items {
		if object, ok := item.(map[string]any); ok {
			if u, ok := object["url"].(string); ok && strings.HasPrefix(u, "http") {
				return u
			}
		}
	}
	return ""
}

func decodeKuaishouJSONString(value string) string {
	decoded, err := strconv.Unquote(`"` + value + `"`)
	if err != nil {
		decoded = value
	}
	return strings.ReplaceAll(decoded, `\/`, "/")
}

func envDurationSeconds(name string, fallback int) time.Duration {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return time.Duration(fallback) * time.Second
	}
	seconds, err := strconv.Atoi(raw)
	if err != nil || seconds <= 0 {
		return time.Duration(fallback) * time.Second
	}
	return time.Duration(seconds) * time.Second
}

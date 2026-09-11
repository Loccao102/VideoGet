package discovery

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

type ollamaGenerateRequest struct {
	Model  string `json:"model"`
	Prompt string `json:"prompt"`
	Stream bool   `json:"stream"`
	Format string `json:"format,omitempty"`
}

type ollamaGenerateResponse struct {
	Response string `json:"response"`
}

type keywordPayload struct {
	Keywords []string `json:"keywords"`
}

// ExpandContext optionally asks Ollama for Chinese search phrases and always falls back to Expand.
// Enable it with KEYWORD_EXPANDER=ollama.
func ExpandContext(ctx context.Context, keyword string) ([]string, error) {
	fallback := Expand(keyword)
	if !strings.EqualFold(strings.TrimSpace(os.Getenv("KEYWORD_EXPANDER")), "ollama") {
		return fallback, nil
	}

	baseURL := strings.TrimRight(strings.TrimSpace(os.Getenv("OLLAMA_BASE_URL")), "/")
	if baseURL == "" {
		baseURL = "http://host.docker.internal:11434"
	}
	model := strings.TrimSpace(os.Getenv("OLLAMA_MODEL"))
	if model == "" {
		model = "qwen3:8b"
	}

	year := time.Now().Year()
	prompt := fmt.Sprintf(
		"Bạn tạo keyword tiếng Trung để săn short video có khả năng dùng cho affiliate trên Kuaishou, Xiaohongshu, Douyin, Weibo, Xigua, Haokan, Toutiao, Bilibili và các nền tảng video Trung Quốc. "+
			"Hiện tại là năm %d. Tuyệt đối không tự thêm năm cũ như 2023, 2024, 2025. Nếu chủ đề không cần năm thì không thêm năm. "+
			"Từ chủ đề người dùng, sinh 4-6 cụm tìm kiếm tiếng Trung ngắn, tự nhiên, ưu tiên intent mua/review/sản phẩm: 好物, 开箱, 测评, 实用, 新品, 爆款, 神器, 黑科技 khi phù hợp. "+
			"Tránh keyword quá chung kiểu 'thảo luận xu hướng' nếu không giúp tìm sản phẩm. Không bịa thương hiệu. "+
			"Chỉ trả JSON hợp lệ dạng {\"keywords\":[\"...\"]}. Chủ đề: %s",
		year,
		keyword,
	)

	body, err := json.Marshal(ollamaGenerateRequest{Model: model, Prompt: prompt, Stream: false, Format: "json"})
	if err != nil {
		return fallback, err
	}

	requestCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(requestCtx, http.MethodPost, baseURL+"/api/generate", bytes.NewReader(body))
	if err != nil {
		return fallback, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fallback, fmt.Errorf("ollama keyword expansion failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fallback, fmt.Errorf("ollama keyword expansion returned %s", resp.Status)
	}

	var generated ollamaGenerateResponse
	if err := json.NewDecoder(resp.Body).Decode(&generated); err != nil {
		return fallback, fmt.Errorf("decode ollama response: %w", err)
	}
	var payload keywordPayload
	if err := json.Unmarshal([]byte(generated.Response), &payload); err != nil {
		return fallback, fmt.Errorf("decode keyword JSON: %w", err)
	}

	return mergeKeywords(keyword, fallback, payload.Keywords), nil
}

func mergeKeywords(original string, groups ...[]string) []string {
	out := []string{strings.TrimSpace(original)}
	seen := map[string]struct{}{strings.ToLower(strings.TrimSpace(original)): {}}
	for _, group := range groups {
		for _, item := range group {
			item = strings.TrimSpace(item)
			if item == "" {
				continue
			}
			key := strings.ToLower(item)
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
			out = append(out, item)
			if len(out) >= 8 {
				return out
			}
		}
	}
	return out
}

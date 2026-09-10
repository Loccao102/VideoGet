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
		model = "qwen2.5:3b"
	}

	prompt := "Bạn tạo keyword để tìm video sản phẩm/trend trên Douyin và Bilibili. " +
		"Từ chủ đề người dùng, hãy sinh 4-6 cụm tìm kiếm tiếng Trung ngắn, tự nhiên, có intent khám phá sản phẩm, review, đồ hay hoặc trend khi phù hợp. " +
		"Không bịa thương hiệu. Chỉ trả JSON hợp lệ dạng {\"keywords\":[\"...\"]}. Chủ đề: " + keyword

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

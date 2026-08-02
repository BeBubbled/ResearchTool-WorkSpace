package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"research-tool-translation-backend/internal/model"
)

type LLMService struct{}

func NewLLMService() *LLMService { return &LLMService{} }

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type chatResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
		FinishReason string `json:"finish_reason"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
	} `json:"usage"`
}

func (s *LLMService) ChatCompletion(ctx context.Context, runtime model.RuntimeModel, content string) (string, model.RequestLog, error) {
	entry := model.RequestLog{Status: "failed", CreatedAt: time.Now()}
	started := time.Now()
	messages := make([]chatMessage, 0, 2)
	if strings.TrimSpace(runtime.SystemPrompt) != "" {
		messages = append(messages, chatMessage{Role: "system", Content: runtime.SystemPrompt})
	}
	messages = append(messages, chatMessage{Role: "user", Content: content})
	body := map[string]any{
		"messages": messages,
		"model":    runtime.Name,
		"stream":   false,
	}
	if runtime.MaxOutputTokens > 0 {
		body["max_tokens"] = runtime.MaxOutputTokens
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		return "", entry, err
	}
	endpoint := strings.TrimRight(runtime.APIBase, "/") + "/chat/completions"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(encoded))
	if err != nil {
		return "", entry, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+runtime.APIKey)
	timeout := time.Duration(runtime.TimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 300 * time.Second
	}
	resp, err := (&http.Client{Timeout: timeout}).Do(req)
	entry.DurationMs = int(time.Since(started).Milliseconds())
	if err != nil {
		return "", entry, err
	}
	defer resp.Body.Close()
	entry.StatusCode = resp.StatusCode
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8*1024*1024))
	entry.DurationMs = int(time.Since(started).Milliseconds())
	if err != nil {
		return "", entry, err
	}
	if resp.StatusCode != http.StatusOK {
		return "", entry, fmt.Errorf("API error %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	var decoded chatResponse
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return "", entry, fmt.Errorf("decode error: %w", err)
	}
	if len(decoded.Choices) == 0 || strings.TrimSpace(decoded.Choices[0].Message.Content) == "" {
		return "", entry, fmt.Errorf("no translated content in response")
	}
	entry.InputTokens = decoded.Usage.PromptTokens
	entry.OutputTokens = decoded.Usage.CompletionTokens
	finishReason := strings.ToLower(strings.TrimSpace(decoded.Choices[0].FinishReason))
	if finishReason == "length" || finishReason == "max_tokens" {
		return "", entry, fmt.Errorf(
			"translation response was truncated by the provider (finish_reason=%s, output_tokens=%d)",
			finishReason,
			entry.OutputTokens,
		)
	}
	if runtime.MaxOutputTokens >= 64 && entry.OutputTokens >= runtime.MaxOutputTokens-16 {
		return "", entry, fmt.Errorf(
			"translation response reached the output-token limit (%d/%d) and may be truncated",
			entry.OutputTokens,
			runtime.MaxOutputTokens,
		)
	}
	if finishReason == "content_filter" {
		return "", entry, fmt.Errorf("translation response was blocked by the provider content filter")
	}
	entry.Status = "success"
	return decoded.Choices[0].Message.Content, entry, nil
}

package service

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"research-tool-translation-backend/internal/model"
)

func TestChatCompletionRejectsTruncatedProviderResponses(t *testing.T) {
	tests := []struct {
		name         string
		finishReason string
		outputTokens int
		errorText    string
	}{
		{name: "finish reason", finishReason: "length", outputTokens: 12, errorText: "finish_reason=length"},
		{name: "token ceiling", finishReason: "stop", outputTokens: 31997, errorText: "output-token limit"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writer.Header().Set("Content-Type", "application/json")
				json.NewEncoder(writer).Encode(map[string]any{
					"choices": []any{map[string]any{
						"message":       map[string]any{"content": "partial translation\n$$\n"},
						"finish_reason": test.finishReason,
					}},
					"usage": map[string]any{"prompt_tokens": 20, "completion_tokens": test.outputTokens},
				})
			}))
			defer server.Close()

			runtime := model.RuntimeModel{
				APIBase: server.URL, APIKey: "secret", Name: "test-model",
				TimeoutSeconds: 10, MaxOutputTokens: 32000,
			}
			translated, entry, err := NewLLMService().ChatCompletion(context.Background(), runtime, "source")
			if err == nil || !strings.Contains(err.Error(), test.errorText) {
				t.Fatalf("expected %q error, got translated=%q entry=%+v err=%v", test.errorText, translated, entry, err)
			}
			if translated != "" {
				t.Fatalf("truncated content must not be returned: %q", translated)
			}
			if entry.Status == "success" {
				t.Fatal("truncated response was marked successful")
			}
		})
	}
}

package service

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"research-tool-translation-backend/internal/model"
)

type recordingSink struct {
	mu       sync.Mutex
	progress []Progress
}

func (s *recordingSink) Log(string) {}

func (s *recordingSink) Progress(value Progress) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.progress = append(s.progress, value)
}

func TestDispatcherTranslatesRawHTMLPersistsChunksWithoutCredentialsAndResumes(t *testing.T) {
	var calls int
	var inputs []string
	var callsMu sync.Mutex
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		callsMu.Lock()
		calls++
		callsMu.Unlock()
		var payload struct {
			Messages []struct {
				Content string `json:"content"`
			} `json:"messages"`
		}
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			t.Errorf("decode request: %v", err)
			writer.WriteHeader(http.StatusBadRequest)
			return
		}
		content := payload.Messages[len(payload.Messages)-1].Content
		callsMu.Lock()
		inputs = append(inputs, content)
		callsMu.Unlock()
		writer.Header().Set("Content-Type", "application/json")
		json.NewEncoder(writer).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": "ZH:" + content}}},
			"usage":   map[string]any{"prompt_tokens": 2, "completion_tokens": 3},
		})
	}))
	defer server.Close()

	root := t.TempDir()
	sourceDir := filepath.Join(root, "source")
	targetDir := filepath.Join(root, "target")
	if err := os.MkdirAll(sourceDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sourceDir, "paper.html"), []byte("<p>one</p>\n<p>two</p>"), 0o644); err != nil {
		t.Fatal(err)
	}
	database := filepath.Join(root, "translation.sqlite3")
	db, err := gorm.Open(sqlite.Open(database), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	sqlDB, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer sqlDB.Close()
	if err := db.AutoMigrate(&model.Task{}, &model.FileJob{}, &model.JobChunk{}, &model.RequestLog{}); err != nil {
		t.Fatal(err)
	}
	sink := &recordingSink{}
	dispatcher := NewDispatcher(db, NewLLMService(), NewSplitter(), sink)
	runtime := model.RuntimeModel{
		APIBase: server.URL, APIKey: "never-store-this-secret", Name: "test-model",
		SystemPrompt: "translate", TimeoutSeconds: 10, MaxInputTokens: 8, MaxOutputTokens: 8,
	}
	task, err := dispatcher.CreateOrResumeTask(model.Task{
		ID: "task-one", SourceDir: sourceDir, TargetDir: targetDir, Concurrency: 2,
	}, []string{"paper.html"}, runtime)
	if err != nil {
		t.Fatal(err)
	}
	if err := dispatcher.Process(context.Background(), task, runtime); err != nil {
		t.Fatal(err)
	}
	translated, err := os.ReadFile(filepath.Join(targetDir, "paper_zh-CN.html"))
	if err != nil {
		t.Fatal(err)
	}
	if string(translated) != "ZH:<p>one</p>\nZH:<p>two</p>\n" {
		t.Fatalf("unexpected merged translation: %q", translated)
	}
	callsMu.Lock()
	firstCalls := calls
	firstInputs := append([]string(nil), inputs...)
	callsMu.Unlock()
	if firstCalls != 2 {
		t.Fatalf("expected two chunk requests, got %d", firstCalls)
	}
	sort.Strings(firstInputs)
	if strings.Join(firstInputs, "") != "<p>one</p>\n<p>two</p>\n" {
		t.Fatalf("HTML was not sent as raw line chunks: %#v", firstInputs)
	}
	if err := dispatcher.Process(context.Background(), task, runtime); err != nil {
		t.Fatal(err)
	}
	callsMu.Lock()
	secondCalls := calls
	callsMu.Unlock()
	if secondCalls != firstCalls {
		t.Fatalf("resume repeated completed requests: before=%d after=%d", firstCalls, secondCalls)
	}
	databaseBytes, err := os.ReadFile(database)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(databaseBytes), runtime.APIKey) {
		t.Fatal("API key was persisted in the translation database")
	}
}

func TestTranslationFileTypesAndOutputNames(t *testing.T) {
	for _, input := range []string{"paper.md", "paper.mmd", "paper.html", "paper.htm"} {
		cleaned, err := cleanRelativePath(input)
		if err != nil {
			t.Fatalf("expected %s to be accepted: %v", input, err)
		}
		ext := filepath.Ext(cleaned)
		expected := strings.TrimSuffix(cleaned, ext) + "_zh-CN" + ext
		if actual := translatedRelativePath(cleaned); actual != expected {
			t.Fatalf("unexpected output name for %s: %s", input, actual)
		}
	}
	if _, err := cleanRelativePath("paper.pdf"); err == nil {
		t.Fatal("expected PDF to remain unsupported")
	}
}

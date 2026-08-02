package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"research-tool-translation-backend/internal/model"
	"research-tool-translation-backend/internal/service"
)

type request struct {
	TaskID      string   `json:"taskId"`
	Database    string   `json:"database"`
	SourceDir   string   `json:"sourceDir"`
	TargetDir   string   `json:"targetDir"`
	Files       []string `json:"files"`
	Concurrency int      `json:"concurrency"`
	LLM         struct {
		BaseURL         string `json:"baseUrl"`
		APIKey          string `json:"apiKey"`
		Model           string `json:"model"`
		SystemPrompt    string `json:"systemPrompt"`
		TimeoutSeconds  int    `json:"timeoutSeconds"`
		MaxInputTokens  int    `json:"maxInputTokens"`
		MaxOutputTokens int    `json:"maxOutputTokens"`
	} `json:"llm"`
}

type jsonSink struct {
	mu     sync.Mutex
	writer *bufio.Writer
}

func (s *jsonSink) emit(value any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	encoded, _ := json.Marshal(value)
	s.writer.Write(encoded)
	s.writer.WriteByte('\n')
	s.writer.Flush()
}

func (s *jsonSink) Log(message string) {
	s.emit(map[string]any{"type": "log", "message": message})
}

func (s *jsonSink) Progress(progress service.Progress) {
	s.emit(map[string]any{
		"type": "progress", "total": progress.Total, "completed": progress.Completed,
		"active": progress.Active, "concurrency": progress.Concurrency,
	})
}

func fail(sink *jsonSink, err error) {
	sink.emit(map[string]any{"type": "error", "message": err.Error()})
	fmt.Fprintln(os.Stderr, err.Error())
	os.Exit(1)
}

func main() {
	sink := &jsonSink{writer: bufio.NewWriter(os.Stdout)}
	var req request
	if err := json.NewDecoder(os.Stdin).Decode(&req); err != nil {
		fail(sink, fmt.Errorf("invalid translation request: %w", err))
	}
	if req.TaskID == "" || req.Database == "" || req.SourceDir == "" || req.TargetDir == "" || len(req.Files) == 0 {
		fail(sink, fmt.Errorf("translation request is missing task paths or files"))
	}
	if req.LLM.BaseURL == "" || req.LLM.APIKey == "" || req.LLM.Model == "" {
		fail(sink, fmt.Errorf("translation request is missing LLM configuration"))
	}
	if err := os.MkdirAll(filepath.Dir(req.Database), 0o755); err != nil {
		fail(sink, err)
	}
	db, err := gorm.Open(sqlite.Open(req.Database+"?_journal_mode=WAL&_busy_timeout=5000"), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		fail(sink, fmt.Errorf("open translation database: %w", err))
	}
	if err := db.AutoMigrate(&model.Task{}, &model.FileJob{}, &model.JobChunk{}, &model.RequestLog{}); err != nil {
		fail(sink, fmt.Errorf("migrate translation database: %w", err))
	}
	dispatcher := service.NewDispatcher(db, service.NewLLMService(), service.NewSplitter(), sink)
	runtime := model.RuntimeModel{
		APIBase: req.LLM.BaseURL, APIKey: req.LLM.APIKey, Name: req.LLM.Model,
		SystemPrompt: req.LLM.SystemPrompt, TimeoutSeconds: req.LLM.TimeoutSeconds,
		MaxInputTokens: req.LLM.MaxInputTokens, MaxOutputTokens: req.LLM.MaxOutputTokens,
	}
	task, err := dispatcher.CreateOrResumeTask(model.Task{
		ID: req.TaskID, SourceDir: req.SourceDir, TargetDir: req.TargetDir, Concurrency: req.Concurrency,
	}, req.Files, runtime)
	if err != nil {
		fail(sink, err)
	}
	sink.Log("AI-Markdown-Translator backend started.")
	if err := dispatcher.Process(context.Background(), task, runtime); err != nil {
		fail(sink, err)
	}
	sink.emit(map[string]any{"type": "complete"})
}

package service

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"gorm.io/gorm"

	"research-tool-translation-backend/internal/model"
)

type Progress struct {
	Total       int
	Completed   int
	Active      int
	Concurrency int
}

type EventSink interface {
	Log(message string)
	Progress(progress Progress)
}

type Dispatcher struct {
	DB       *gorm.DB
	LLM      *LLMService
	Splitter *Splitter
	Sink     EventSink
}

func NewDispatcher(db *gorm.DB, llm *LLMService, splitter *Splitter, sink EventSink) *Dispatcher {
	d := &Dispatcher{DB: db, LLM: llm, Splitter: splitter, Sink: sink}
	d.CleanUpOnStartup()
	return d
}

func (d *Dispatcher) CleanUpOnStartup() {
	d.DB.Model(&model.Task{}).Where("status = ?", "running").Update("status", "paused")
	d.DB.Model(&model.FileJob{}).Where("status = ?", "processing").Update("status", "pending")
	d.DB.Model(&model.JobChunk{}).Where("status = ?", "processing").Update("status", "pending")
}

func cleanRelativePath(value string) (string, error) {
	cleaned := filepath.Clean(value)
	if filepath.IsAbs(cleaned) || cleaned == "." || cleaned == ".." || strings.HasPrefix(cleaned, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("unsafe relative path: %s", value)
	}
	ext := strings.ToLower(filepath.Ext(cleaned))
	if ext != ".md" && ext != ".mmd" && ext != ".html" && ext != ".htm" {
		return "", fmt.Errorf("unsupported translation file: %s", value)
	}
	return cleaned, nil
}

func translatedRelativePath(relative string) string {
	ext := filepath.Ext(relative)
	return strings.TrimSuffix(relative, ext) + "_zh-CN" + ext
}

func withinRoot(root, candidate string) bool {
	relative, err := filepath.Rel(root, candidate)
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) && !filepath.IsAbs(relative)
}

func (d *Dispatcher) CreateOrResumeTask(req model.Task, files []string, runtime model.RuntimeModel) (*model.Task, error) {
	var existing model.Task
	lookup := d.DB.First(&existing, "id = ?", req.ID)
	if lookup.Error == nil {
		if filepath.Clean(existing.SourceDir) != filepath.Clean(req.SourceDir) || filepath.Clean(existing.TargetDir) != filepath.Clean(req.TargetDir) {
			return nil, fmt.Errorf("saved task paths do not match this request")
		}
		existing.Concurrency = req.Concurrency
		existing.Status = "created"
		if err := d.DB.Save(&existing).Error; err != nil {
			return nil, err
		}
		return &existing, nil
	}
	if lookup.Error != gorm.ErrRecordNotFound {
		return nil, lookup.Error
	}
	if req.ID == "" {
		req.ID = uuid.NewString()
	}
	sourceInfo, err := os.Stat(req.SourceDir)
	if err != nil || !sourceInfo.IsDir() {
		return nil, fmt.Errorf("invalid source directory")
	}
	if err := os.MkdirAll(req.TargetDir, 0o755); err != nil {
		return nil, err
	}
	if runtime.MaxInputTokens <= 0 {
		runtime.MaxInputTokens = 96000
	}
	safeLimit := runtime.MaxInputTokens
	if runtime.MaxOutputTokens > 0 && runtime.MaxOutputTokens < safeLimit {
		safeLimit = runtime.MaxOutputTokens
	}
	prepared := make([]string, 0, len(files))
	for _, value := range files {
		relative, err := cleanRelativePath(value)
		if err != nil {
			return nil, err
		}
		prepared = append(prepared, relative)
	}
	sort.Strings(prepared)
	req.Status = "created"
	req.CreatedAt = time.Now()
	req.UpdatedAt = time.Now()
	err = d.DB.Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(&req).Error; err != nil {
			return err
		}
		for _, relative := range prepared {
			sourcePath := filepath.Join(req.SourceDir, relative)
			if !withinRoot(req.SourceDir, sourcePath) {
				return fmt.Errorf("source escapes translation root: %s", relative)
			}
			content, err := os.ReadFile(sourcePath)
			if err != nil {
				return err
			}
			fileJob := model.FileJob{
				ID:         uuid.NewString(),
				TaskID:     req.ID,
				FilePath:   relative,
				OutputPath: translatedRelativePath(relative),
				Status:     "pending",
				CreatedAt:  time.Now(),
				UpdatedAt:  time.Now(),
			}
			if strings.TrimSpace(string(content)) == "" {
				fileJob.Status = "completed"
				outputPath := filepath.Join(req.TargetDir, fileJob.OutputPath)
				if !withinRoot(req.TargetDir, outputPath) {
					return fmt.Errorf("output escapes translation root: %s", fileJob.OutputPath)
				}
				if err := os.MkdirAll(filepath.Dir(outputPath), 0o755); err != nil {
					return err
				}
				if err := os.WriteFile(outputPath, nil, 0o644); err != nil {
					return err
				}
			}
			if err := tx.Create(&fileJob).Error; err != nil {
				return err
			}
			if fileJob.Status != "completed" {
				for index, chunkContent := range d.Splitter.Split(string(content), safeLimit) {
					chunk := model.JobChunk{
						ID: uuid.NewString(), FileJobID: fileJob.ID, Sequence: index,
						Content: chunkContent, Status: "pending", CreatedAt: time.Now(), UpdatedAt: time.Now(),
					}
					if err := tx.Create(&chunk).Error; err != nil {
						return err
					}
				}
			}
		}
		req.TotalFiles = len(prepared)
		return tx.Save(&req).Error
	})
	if err != nil {
		return nil, err
	}
	return &req, nil
}

func (d *Dispatcher) Process(ctx context.Context, task *model.Task, runtime model.RuntimeModel) error {
	concurrency := task.Concurrency
	if concurrency <= 0 {
		concurrency = 1
	}
	d.DB.Model(&model.FileJob{}).Where("task_id = ? AND status = ?", task.ID, "failed").Updates(map[string]any{"status": "pending", "error_msg": ""})
	var fileIDs []string
	d.DB.Model(&model.FileJob{}).Where("task_id = ?", task.ID).Pluck("id", &fileIDs)
	if len(fileIDs) > 0 {
		d.DB.Model(&model.JobChunk{}).Where("file_job_id IN ? AND status IN ?", fileIDs, []string{"failed", "processing"}).Updates(map[string]any{"status": "pending", "error_msg": ""})
	}
	var chunks []model.JobChunk
	if len(fileIDs) > 0 {
		if err := d.DB.Where("file_job_id IN ? AND status = ?", fileIDs, "pending").Order("file_job_id, sequence").Find(&chunks).Error; err != nil {
			return err
		}
	}
	var total, completed int64
	d.DB.Model(&model.JobChunk{}).Where("file_job_id IN ?", fileIDs).Count(&total)
	d.DB.Model(&model.JobChunk{}).Where("file_job_id IN ? AND status = ?", fileIDs, "completed").Count(&completed)
	task.Status = "running"
	d.DB.Save(task)
	progress := Progress{Total: int(total), Completed: int(completed), Concurrency: concurrency}
	d.Sink.Progress(progress)

	jobs := make(chan model.JobChunk)
	var workers sync.WaitGroup
	var state sync.Mutex
	failed := false
	for index := 0; index < concurrency; index++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for chunk := range jobs {
				state.Lock()
				progress.Active++
				d.Sink.Progress(progress)
				state.Unlock()
				d.processChunk(ctx, task, runtime, &chunk)
				state.Lock()
				progress.Active--
				var refreshed model.JobChunk
				d.DB.First(&refreshed, "id = ?", chunk.ID)
				if refreshed.Status == "completed" {
					progress.Completed++
				} else {
					failed = true
				}
				d.Sink.Progress(progress)
				state.Unlock()
			}
		}()
	}
	for _, chunk := range chunks {
		select {
		case <-ctx.Done():
			close(jobs)
			workers.Wait()
			return ctx.Err()
		case jobs <- chunk:
		}
	}
	close(jobs)
	workers.Wait()
	for _, fileID := range fileIDs {
		if err := d.mergeFile(fileID, task.TargetDir); err != nil {
			failed = true
			d.Sink.Log(err.Error())
		}
	}
	if failed {
		task.Status = "failed"
		d.DB.Save(task)
		return fmt.Errorf("one or more translation chunks failed")
	}
	task.Status = "completed"
	task.ProcessedFiles = task.TotalFiles
	d.DB.Save(task)
	return nil
}

func (d *Dispatcher) processChunk(ctx context.Context, task *model.Task, runtime model.RuntimeModel, chunk *model.JobChunk) {
	d.DB.Model(chunk).Updates(map[string]any{"status": "processing", "updated_at": time.Now()})
	translated, entry, err := d.LLM.ChatCompletion(ctx, runtime, chunk.Content)
	entry.TaskID = task.ID
	entry.FileJobID = chunk.FileJobID
	d.DB.Create(&entry)
	updates := map[string]any{
		"translated": translated, "status": "completed", "updated_at": time.Now(),
		"input_tokens": entry.InputTokens, "output_tokens": entry.OutputTokens,
		"duration_ms": entry.DurationMs, "error_msg": "",
	}
	if err != nil {
		safeError := strings.ReplaceAll(err.Error(), runtime.APIKey, "[redacted]")
		updates["status"] = "failed"
		updates["error_msg"] = safeError
		d.DB.Model(&model.FileJob{}).Where("id = ?", chunk.FileJobID).Updates(map[string]any{"status": "failed", "error_msg": safeError})
		d.Sink.Log(fmt.Sprintf("Chunk %d failed: %s", chunk.Sequence+1, safeError))
	}
	d.DB.Model(chunk).Updates(updates)
}

func (d *Dispatcher) mergeFile(fileJobID, targetDir string) error {
	return d.DB.Transaction(func(tx *gorm.DB) error {
		var fileJob model.FileJob
		if err := tx.First(&fileJob, "id = ?", fileJobID).Error; err != nil {
			return err
		}
		var remaining int64
		tx.Model(&model.JobChunk{}).Where("file_job_id = ? AND status != ?", fileJobID, "completed").Count(&remaining)
		if remaining != 0 {
			return nil
		}
		var chunks []model.JobChunk
		if err := tx.Where("file_job_id = ?", fileJobID).Order("sequence").Find(&chunks).Error; err != nil {
			return err
		}
		var output strings.Builder
		inputTokens, outputTokens, durationMs := 0, 0, 0
		for _, chunk := range chunks {
			output.WriteString(chunk.Translated)
			inputTokens += chunk.InputTokens
			outputTokens += chunk.OutputTokens
			durationMs += chunk.DurationMs
		}
		outputPath := filepath.Join(targetDir, fileJob.OutputPath)
		if !withinRoot(targetDir, outputPath) {
			return fmt.Errorf("output escapes translation root: %s", fileJob.OutputPath)
		}
		if err := os.MkdirAll(filepath.Dir(outputPath), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(outputPath, []byte(output.String()), 0o644); err != nil {
			return err
		}
		return tx.Model(&fileJob).Updates(map[string]any{
			"status": "completed", "input_tokens": inputTokens, "output_tokens": outputTokens,
			"duration_ms": durationMs, "error_msg": "",
		}).Error
	})
}

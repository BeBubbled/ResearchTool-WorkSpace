package model

import "time"

// Task, FileJob, JobChunk, and RequestLog intentionally mirror the durable
// backend records from GMYXDS/AI-Markdown-Translator. LLM credentials are not
// part of this schema: the Research Toolbox remains their only owner.
type Task struct {
	ID             string `gorm:"primaryKey"`
	SourceDir      string `gorm:"not null"`
	TargetDir      string `gorm:"not null"`
	Concurrency    int    `gorm:"default:1"`
	Status         string `gorm:"default:'created'"`
	TotalFiles     int    `gorm:"default:0"`
	ProcessedFiles int    `gorm:"default:0"`
	CreatedAt      time.Time
	UpdatedAt      time.Time
}

type FileJob struct {
	ID           string `gorm:"primaryKey"`
	TaskID       string `gorm:"not null;index"`
	FilePath     string `gorm:"not null"`
	OutputPath   string `gorm:"not null"`
	Status       string `gorm:"default:'pending'"`
	InputTokens  int    `gorm:"default:0"`
	OutputTokens int    `gorm:"default:0"`
	DurationMs   int    `gorm:"default:0"`
	ErrorMsg     string
	CreatedAt    time.Time
	UpdatedAt    time.Time
}

type JobChunk struct {
	ID           string `gorm:"primaryKey"`
	FileJobID    string `gorm:"not null;index"`
	Sequence     int    `gorm:"not null"`
	Content      string
	Translated   string
	Status       string `gorm:"default:'pending'"`
	InputTokens  int    `gorm:"default:0"`
	OutputTokens int    `gorm:"default:0"`
	DurationMs   int    `gorm:"default:0"`
	ErrorMsg     string
	CreatedAt    time.Time
	UpdatedAt    time.Time
}

type RequestLog struct {
	ID           uint   `gorm:"primaryKey"`
	TaskID       string `gorm:"index"`
	FileJobID    string `gorm:"index"`
	InputTokens  int
	OutputTokens int
	DurationMs   int
	StatusCode   int
	Status       string
	CreatedAt    time.Time
}

type RuntimeModel struct {
	APIBase         string
	APIKey          string
	Name            string
	SystemPrompt    string
	TimeoutSeconds  int
	MaxInputTokens  int
	MaxOutputTokens int
}

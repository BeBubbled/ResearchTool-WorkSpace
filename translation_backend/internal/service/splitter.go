package service

import (
	"strings"
	"unicode/utf8"
)

// Splitter is the line-oriented splitter used by AI-Markdown-Translator.
// It deliberately does not parse or protect Markdown syntax.
type Splitter struct{}

func NewSplitter() *Splitter { return &Splitter{} }

func (s *Splitter) EstimateTokens(text string) int {
	return utf8.RuneCountInString(text)
}

func (s *Splitter) Split(content string, maxInputTokens int) []string {
	if maxInputTokens <= 0 {
		maxInputTokens = 4000
	}
	limit := int(float64(maxInputTokens) * 0.9)
	if limit < 1 {
		limit = 1
	}

	lines := strings.Split(content, "\n")
	chunks := make([]string, 0)
	var current strings.Builder
	currentTokens := 0
	for _, line := range lines {
		lineWithNewline := line + "\n"
		tokens := s.EstimateTokens(lineWithNewline)
		if currentTokens+tokens > limit && current.Len() > 0 {
			chunks = append(chunks, current.String())
			current.Reset()
			currentTokens = 0
		}
		current.WriteString(lineWithNewline)
		currentTokens += tokens
	}
	if current.Len() > 0 {
		chunks = append(chunks, current.String())
	}
	return chunks
}

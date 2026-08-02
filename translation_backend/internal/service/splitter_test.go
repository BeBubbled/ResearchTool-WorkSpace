package service

import "testing"

func TestSplitterKeepsLineBoundaries(t *testing.T) {
	chunks := NewSplitter().Split("alpha\nbeta\ngamma", 14)
	if len(chunks) != 2 {
		t.Fatalf("expected 2 chunks, got %d: %#v", len(chunks), chunks)
	}
	if chunks[0] != "alpha\nbeta\n" || chunks[1] != "gamma\n" {
		t.Fatalf("unexpected chunks: %#v", chunks)
	}
}

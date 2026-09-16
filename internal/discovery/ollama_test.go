package discovery

import "testing"

func TestLimitKeywordsUsesConfiguredCap(t *testing.T) {
	t.Setenv("SEARCH_KEYWORD_LIMIT", "3")
	got := limitKeywords([]string{"a", "b", "c", "d", "e"})
	if len(got) != 3 {
		t.Fatalf("len(got) = %d, want 3", len(got))
	}
	if got[0] != "a" || got[2] != "c" {
		t.Fatalf("unexpected keyword order: %v", got)
	}
}

func TestMergeKeywordsRespectsConfiguredCap(t *testing.T) {
	t.Setenv("SEARCH_KEYWORD_LIMIT", "4")
	got := mergeKeywords("root", []string{"one", "two", "three", "four", "five"})
	if len(got) != 4 {
		t.Fatalf("len(got) = %d, want 4: %v", len(got), got)
	}
	if got[0] != "root" {
		t.Fatalf("original keyword must remain first: %v", got)
	}
}

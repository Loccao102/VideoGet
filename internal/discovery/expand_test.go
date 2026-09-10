package discovery

import "testing"

func TestExpandKitchenVietnamese(t *testing.T) {
	got := Expand("đồ bếp")
	if len(got) < 2 {
		t.Fatalf("expected expanded keywords, got %v", got)
	}
	if got[0] != "đồ bếp" {
		t.Fatalf("original keyword must remain first, got %q", got[0])
	}
	found := false
	for _, item := range got {
		if item == "厨房好物" {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("expected 厨房好物 in %v", got)
	}
}

func TestExpandUnknownKeepsOriginal(t *testing.T) {
	got := Expand("một chủ đề rất riêng")
	if len(got) != 1 || got[0] != "một chủ đề rất riêng" {
		t.Fatalf("unexpected expansion: %v", got)
	}
}

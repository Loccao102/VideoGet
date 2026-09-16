package source

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type blockingProvider struct{}

func (blockingProvider) Name() string { return "blocking" }
func (blockingProvider) Available() error { return nil }
func (blockingProvider) Search(ctx context.Context, _ string, _ int) ([]model.Video, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

func TestWithSearchTimeoutReturnsStructuredTimeout(t *testing.T) {
	provider := WithSearchTimeout(blockingProvider{}, 20*time.Millisecond)
	started := time.Now()
	_, err := provider.Search(context.Background(), "x", 1)
	if err == nil {
		t.Fatal("expected timeout error")
	}
	if !strings.Contains(err.Error(), "blocking search timed out") {
		t.Fatalf("unexpected error: %v", err)
	}
	if time.Since(started) > 500*time.Millisecond {
		t.Fatalf("timeout wrapper returned too slowly")
	}
}

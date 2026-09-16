package source

import (
	"context"
	"fmt"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type searchTimeoutProvider struct {
	provider Provider
	timeout  time.Duration
}

// WithSearchTimeout guarantees that a provider call returns when its context/time
// budget expires, even if an underlying adapter is temporarily blocked in a
// browser/process cleanup path. The inner call receives the same cancelled
// context so well-behaved providers still stop their own work normally.
func WithSearchTimeout(provider Provider, timeout time.Duration) Provider {
	if provider == nil || timeout <= 0 {
		return provider
	}
	return &searchTimeoutProvider{provider: provider, timeout: timeout}
}

func (p *searchTimeoutProvider) Name() string { return p.provider.Name() }

func (p *searchTimeoutProvider) Available() error { return p.provider.Available() }

func (p *searchTimeoutProvider) Search(ctx context.Context, keyword string, limit int) ([]model.Video, error) {
	searchCtx, cancel := context.WithTimeout(ctx, p.timeout)
	defer cancel()

	type result struct {
		videos []model.Video
		err    error
	}
	resultCh := make(chan result, 1)
	go func() {
		videos, err := p.provider.Search(searchCtx, keyword, limit)
		resultCh <- result{videos: videos, err: err}
	}()

	select {
	case item := <-resultCh:
		return item.videos, item.err
	case <-searchCtx.Done():
		return nil, fmt.Errorf("%s search timed out after %s: %w", p.Name(), p.timeout, searchCtx.Err())
	}
}

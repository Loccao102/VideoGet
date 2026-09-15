package source

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/Loccao102/VideoGet/internal/model"
)

type Provider interface {
	Name() string
	Available() error
	Search(ctx context.Context, keyword string, limit int) ([]model.Video, error)
}

func executable(name string) (string, error) {
	path, err := exec.LookPath(name)
	if err != nil { return "", fmt.Errorf("required executable %q was not found in PATH: %w", name, err) }
	if path == "" { return "", errors.New("executable path is empty") }
	return path, nil
}

// envSeconds is shared by source adapters. Keep it outside a platform-specific
// provider so removing or replacing one source cannot break the others.
func envSeconds(name string, fallback int) time.Duration {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return time.Duration(fallback) * time.Second
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return time.Duration(fallback) * time.Second
	}
	return time.Duration(parsed) * time.Second
}

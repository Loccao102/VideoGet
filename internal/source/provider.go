package source

import (
	"context"
	"errors"
	"fmt"
	"os/exec"

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

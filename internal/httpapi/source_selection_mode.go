package httpapi

import (
	"os"
	"strings"
)

// The current UI exposes every provider as an explicit multi-select. In exact
// selection mode a request such as ["douyin"] must mean Douyin only; legacy
// AUTO_FREE_SOURCES expansion would otherwise silently add public providers.
//
// Set SOURCE_SELECTION_EXACT=false to restore the old automatic expansion
// behaviour for deployments that still rely on it.
func init() {
	value := strings.ToLower(strings.TrimSpace(os.Getenv("SOURCE_SELECTION_EXACT")))
	if value == "0" || value == "false" || value == "no" || value == "off" {
		return
	}
	_ = os.Setenv("AUTO_FREE_SOURCES", "false")
}

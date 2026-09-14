package download

import "testing"

func TestNormalizeSpeechRate(t *testing.T) {
	cases := map[string]string{
		"":      "auto",
		"auto":  "auto",
		"+20%":  "+20%",
		"20":    "+20%",
		"-10%":  "-10%",
		"+100%": "+100%",
	}
	for input, want := range cases {
		got, err := normalizeSpeechRate(input)
		if err != nil {
			t.Fatalf("normalizeSpeechRate(%q): %v", input, err)
		}
		if got != want {
			t.Fatalf("normalizeSpeechRate(%q)=%q, want %q", input, got, want)
		}
	}
	for _, input := range []string{"abc", "+101%", "-51%"} {
		if _, err := normalizeSpeechRate(input); err == nil {
			t.Fatalf("normalizeSpeechRate(%q) should fail", input)
		}
	}
}

func TestSubtitleSRTTime(t *testing.T) {
	cases := map[float64]string{
		0:       "00:00:00,000",
		1.234:   "00:00:01,234",
		61.005:  "00:01:01,005",
		3661.75: "01:01:01,750",
	}
	for input, want := range cases {
		if got := subtitleSRTTime(input); got != want {
			t.Fatalf("subtitleSRTTime(%v)=%q, want %q", input, got, want)
		}
	}
}

#!/usr/bin/env python3
"""One-shot localization entrypoint with smart preserve-frame rendering."""
import json
import sys
from pathlib import Path

import localize as base
import localize_fast as fast  # installs fast translation/TTS overrides on base
import smart_render

base.render_video = smart_render.render_video


def input_arg() -> str:
    try:
        index = sys.argv.index("--input")
        return str(Path(sys.argv[index + 1]).resolve())
    except (ValueError, IndexError):
        return ""


if __name__ == "__main__":
    try:
        base.main()
    except RuntimeError as error:
        if "Whisper did not detect any speech" in str(error):
            print(
                json.dumps(
                    {
                        "outputVideo": input_arg(),
                        "segments": 0,
                        "skippedReason": "no_speech",
                    },
                    ensure_ascii=False,
                )
            )
        else:
            base.log(f"ERROR: {error}")
            sys.exit(1)
    except Exception as error:
        base.log(f"ERROR: {error}")
        sys.exit(1)

#!/usr/bin/env python3
"""Compatibility entrypoint that upgrades subtitle retranslation to V2.2."""
import retranslate_subtitles as legacy
import utterance_translate_v22 as v22

legacy.contextual_translate = v22

if __name__ == "__main__":
    legacy.main()

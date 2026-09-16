# Local music library

Put royalty-free or otherwise licensed background music files in this folder for `ocr_music` jobs.

Supported extensions: `.mp3`, `.m4a`, `.aac`, `.wav`, `.flac`, `.ogg`, `.opus`.

VideoGet chooses a deterministic track per input video when `OCR_MUSIC_FILE` is blank. To force one track, set:

```env
OCR_MUSIC_FILE=/app/assets/music/my-track.mp3
```

Do not commit copyrighted audio that you do not have permission to redistribute.

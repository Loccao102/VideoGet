# Sample-driven OCR overlay fixes

The supplied Bilibili sample exposed three concrete issues in the previous initial OCR output:

1. Vietnamese captions were rendered above the Chinese source caption instead of covering it.
2. The Chinese source caption remained visible below the Vietnamese caption.
3. The uploader + `bilibili` watermark at the top-right remained visible.

The default OCR subtitle output now uses the per-segment Overlay renderer immediately after OCR/translation instead of waiting for a manual re-render action. Lower captions use an expanded black display box centered on the source OCR bbox; upper/middle captions keep the blur-at-source-position behavior. The built-in Bilibili cleanup default targets the top-right watermark area.

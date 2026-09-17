#!/usr/bin/env python3
"""Generate the default Xứ Sở Nhiều Lông transparent watermark badge."""
from __future__ import annotations

import os
from pathlib import Path


def _font(size: int):
    from PIL import ImageFont
    candidates = [
        os.getenv("BRAND_WATERMARK_FONT", "").strip(),
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def generate(output_path: Path, text: str = "Xứ Sở Nhiều Lông") -> Path:
    from PIL import Image, ImageDraw

    width, height = 1100, 300
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    gold = (255, 184, 55, 255)
    dark = (20, 18, 17, 244)
    white = (250, 250, 250, 255)
    pink = (255, 183, 178, 255)
    gray = (125, 125, 130, 255)
    cream = (255, 190, 84, 255)

    draw.rounded_rectangle((18, 72, 1082, 244), radius=86, fill=dark, outline=gold, width=8)

    # Cat head.
    draw.polygon([(62, 130), (92, 82), (116, 132)], fill=gray, outline=(35, 27, 22, 255))
    draw.polygon([(142, 132), (169, 82), (194, 138)], fill=gray, outline=(35, 27, 22, 255))
    draw.ellipse((68, 104, 194, 234), fill=gray, outline=(35, 27, 22, 255), width=6)
    draw.ellipse((87, 132, 176, 224), fill=(238, 236, 232, 255))
    draw.arc((96, 144, 126, 173), 200, 340, fill=(25, 20, 18, 255), width=5)
    draw.arc((140, 144, 170, 173), 200, 340, fill=(25, 20, 18, 255), width=5)
    draw.ellipse((129, 166, 140, 177), fill=pink)
    draw.arc((118, 167, 151, 197), 20, 160, fill=(25, 20, 18, 255), width=4)

    # Dog head.
    draw.ellipse((175, 88, 314, 232), fill=cream, outline=(46, 30, 18, 255), width=6)
    draw.ellipse((157, 114, 202, 211), fill=(221, 145, 49, 255), outline=(46, 30, 18, 255), width=5)
    draw.ellipse((288, 114, 330, 211), fill=(221, 145, 49, 255), outline=(46, 30, 18, 255), width=5)
    draw.arc((207, 132, 239, 165), 200, 340, fill=(35, 23, 16, 255), width=5)
    draw.arc((254, 132, 286, 165), 200, 340, fill=(35, 23, 16, 255), width=5)
    draw.ellipse((239, 161, 258, 179), fill=(50, 31, 23, 255))
    draw.arc((226, 170, 274, 214), 10, 170, fill=(50, 31, 23, 255), width=5)

    # Paw mark.
    draw.ellipse((1020, 132, 1046, 158), fill=gold)
    draw.ellipse((1047, 120, 1070, 148), fill=gold)
    draw.ellipse((1040, 167, 1073, 199), fill=gold)
    draw.ellipse((1003, 167, 1036, 199), fill=gold)
    draw.ellipse((1016, 150, 1059, 193), fill=gold)

    font = _font(64)
    split = "Nhiều Lông"
    prefix = text[:-len(split)].rstrip() + " " if text.endswith(split) else text + " "
    x, y = 335, 116
    draw.text((x, y), prefix, font=font, fill=white, stroke_width=2, stroke_fill=(0, 0, 0, 140))
    prefix_box = draw.textbbox((x, y), prefix, font=font, stroke_width=2)
    draw.text((prefix_box[2] + 4, y), split if text.endswith(split) else "", font=font, fill=gold, stroke_width=2, stroke_fill=(0, 0, 0, 140))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, optimize=True)
    return output_path


def ensure(output_dir: Path) -> Path | None:
    if os.getenv("BRAND_WATERMARK_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return None
    asset = os.getenv("BRAND_WATERMARK_ASSET", "").strip()
    if asset:
        path = Path(asset)
        if path.exists() and path.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg"}:
            return path
    output = output_dir / ".videoget-brand-xu-so-nhieu-long.png"
    if output.exists() and output.stat().st_size > 0:
        return output
    return generate(output, os.getenv("BRAND_WATERMARK_TEXT", "Xứ Sở Nhiều Lông").strip() or "Xứ Sở Nhiều Lông")

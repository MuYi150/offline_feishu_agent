from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


PAGE_SIZE = (1240, 1754)
MARGIN_X = 96
MARGIN_TOP = 90
MARGIN_BOTTOM = 90
CONTENT_WIDTH = PAGE_SIZE[0] - 2 * MARGIN_X
FONT_REGULAR = Path(r"C:\Windows\Fonts\msyh.ttc")
FONT_BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    if not path.is_file():
        raise SystemExit(f"required Chinese font not found: {path}")
    return ImageFont.truetype(str(path), size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        if current and draw.textlength(candidate, font=font) > width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current or not lines:
        lines.append(current)
    return lines


class PageWriter:
    def __init__(self) -> None:
        self.pages: list[Image.Image] = []
        self.image = Image.new("RGB", PAGE_SIZE, "white")
        self.draw = ImageDraw.Draw(self.image)
        self.y = MARGIN_TOP

    def _finish_page(self) -> None:
        self.pages.append(self.image)
        self.image = Image.new("RGB", PAGE_SIZE, "white")
        self.draw = ImageDraw.Draw(self.image)
        self.y = MARGIN_TOP

    def ensure(self, height: int) -> None:
        if self.y + height > PAGE_SIZE[1] - MARGIN_BOTTOM and self.y > MARGIN_TOP:
            self._finish_page()

    def text(self, content: str, *, font: ImageFont.FreeTypeFont, spacing: int, before: int, after: int) -> None:
        lines = _wrap(self.draw, content, font, CONTENT_WIDTH)
        line_height = font.size + spacing
        block_height = before + len(lines) * line_height + after
        self.ensure(block_height)
        self.y += before
        for line in lines:
            self.draw.text((MARGIN_X, self.y), line, font=font, fill="#202124")
            self.y += line_height
        self.y += after

    def picture(self, path: Path, caption: str, *, caption_font: ImageFont.FreeTypeFont) -> None:
        with Image.open(path) as source:
            picture = source.convert("RGB")
        max_width = CONTENT_WIDTH
        max_height = 760
        ratio = min(max_width / picture.width, max_height / picture.height, 1.0)
        target_size = (max(1, round(picture.width * ratio)), max(1, round(picture.height * ratio)))
        picture = picture.resize(target_size, Image.Resampling.LANCZOS)
        caption_height = caption_font.size + 28
        self.ensure(picture.height + caption_height + 40)
        x = (PAGE_SIZE[0] - picture.width) // 2
        self.draw.rounded_rectangle(
            (x - 3, self.y - 3, x + picture.width + 3, self.y + picture.height + 3),
            radius=4,
            outline="#c7c9cc",
            width=2,
        )
        self.image.paste(picture, (x, self.y))
        self.y += picture.height + 16
        caption_width = self.draw.textlength(caption, font=caption_font)
        self.draw.text(((PAGE_SIZE[0] - caption_width) / 2, self.y), caption, font=caption_font, fill="#5f6368")
        self.y += caption_height

    def save(self, output: Path) -> None:
        if self.y > MARGIN_TOP or not self.pages:
            self._finish_page()
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            raise SystemExit(f"refusing to write into non-empty pages directory: {output}")
        footer_font = _font(FONT_REGULAR, 16)
        total = len(self.pages)
        for index, page in enumerate(self.pages, 1):
            draw = ImageDraw.Draw(page)
            footer = f"离线审稿 Fixture 页面  {index} / {total}"
            footer_width = draw.textlength(footer, font=footer_font)
            draw.text(((PAGE_SIZE[0] - footer_width) / 2, PAGE_SIZE[1] - 54), footer, font=footer_font, fill="#80868b")
            page.save(output / f"page-{index:04d}.png", optimize=True)


def render_fixture(case: Path) -> None:
    blocks = _load_json(case / "document_blocks.json")["blocks"]
    manifest = _load_json(case / "image_manifest.json")
    image_paths = {item["token"]: case / item["path"] for item in manifest["images"]}
    fonts = {
        "heading1": _font(FONT_BOLD, 34),
        "heading2": _font(FONT_BOLD, 27),
        "body": _font(FONT_REGULAR, 22),
        "caption": _font(FONT_REGULAR, 18),
    }
    writer = PageWriter()
    ordered_index = 0
    image_index = 0
    previous_type = ""
    for block in blocks:
        block_type = block.get("block_type", "")
        if block_type == "image":
            ordered_index = 0
            image_index += 1
            token = block.get("image", {}).get("token", "")
            image_path = image_paths.get(token)
            if image_path is None or not image_path.is_file():
                raise SystemExit(f"image token is unavailable: {token}")
            writer.picture(image_path, f"附图 {image_index}", caption_font=fonts["caption"])
        elif block_type in {"heading1", "heading2", "text", "ordered"}:
            elements = block.get(block_type, {}).get("elements", [])
            content = "".join(item.get("text_run", {}).get("content", "") for item in elements).strip()
            if not content:
                continue
            if block_type == "ordered":
                ordered_index = ordered_index + 1 if previous_type == "ordered" else 1
                content = f"{ordered_index}. {content}"
                writer.text(content, font=fonts["body"], spacing=10, before=3, after=8)
            elif block_type == "heading1":
                ordered_index = 0
                writer.text(content, font=fonts["heading1"], spacing=12, before=24, after=18)
            elif block_type == "heading2":
                ordered_index = 0
                writer.text(content, font=fonts["heading2"], spacing=10, before=18, after=12)
            else:
                ordered_index = 0
                writer.text(content, font=fonts["body"], spacing=11, before=3, after=12)
        previous_type = block_type
    writer.save(case / "pages")
    print(f"pages={len(writer.pages)} output={(case / 'pages').resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render v1-compatible Fixture blocks and images into page PNG files")
    parser.add_argument("case", type=Path)
    args = parser.parse_args()
    render_fixture(args.case)


if __name__ == "__main__":
    main()

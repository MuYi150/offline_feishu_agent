"""Deterministically regenerate the committed PDF fixtures with PyMuPDF/Pillow."""

from __future__ import annotations

from pathlib import Path

import fitz
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent


def generate_long_text() -> None:
    target = ROOT / "long_pdf" / "source.pdf"
    document = fitz.open()
    for page_number in range(1, 17):
        page = document.new_page(width=595, height=842)
        text = (
            f"第 {page_number} 页：本页记录实验环境、依赖版本、执行命令、参数配置、"
            "观察日志、故障排查过程以及阶段结论，全部内容来自 PDF 原生文字层。"
        )
        page.insert_textbox(
            fitz.Rect(54, 80, 541, 220),
            text,
            fontname="china-s",
            fontsize=12,
            lineheight=1.5,
        )
    document.save(target)
    document.close()


def _image(path: Path, *, label: str, variant: int = 0, scanned: bool = False) -> None:
    image = Image.new("RGB", (900, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 860, 560), outline=(20, 60, 140), width=8)
    for index in range(8):
        y = 90 + index * 50
        draw.line((90, y, 760 - variant * 10, y + (index % 3) * 15), fill=(30, 90, 180), width=5)
    draw.ellipse((650, 150, 820, 320), fill=(230, 120, 40), outline="black", width=4)
    draw.text((100, 470), label, fill="black")
    if scanned:
        for index in range(8):
            draw.text((100, 70 + index * 55), f"SCANNED RECORD LINE {index + 1}", fill="black")
    image.save(path)


def generate_long_mixed() -> None:
    fixture = ROOT / "long_pdf_mixed"
    fixture.mkdir(parents=True, exist_ok=True)
    asset = fixture / "_embedded.png"
    scan = fixture / "_scan.png"
    _image(asset, label="TEST RESULT CURVE")
    _image(scan, label="SCANNED PAGE", scanned=True)

    document = fitz.open()
    for page_number in range(1, 15):
        page = document.new_page(width=595, height=842)
        if page_number == 7:
            page.insert_image(page.rect, filename=str(scan))
            continue
        page.insert_textbox(
            fitz.Rect(54, 45, 541, 125),
            f"第 {page_number} 页：长篇混合 PDF 的实验记录包含配置步骤、验证数据、测试结果和结论。",
            fontname="china-s",
            fontsize=11,
        )
        if page_number in {3, 8, 9}:
            page.insert_image(fitz.Rect(100, 180, 500, 447), filename=str(asset))
        if page_number == 4:
            x_positions = [80, 230, 380, 520]
            y_positions = [180, 230, 280, 330, 380]
            for x in x_positions:
                page.draw_line((x, y_positions[0]), (x, y_positions[-1]), color=(0, 0, 0))
            for y in y_positions:
                page.draw_line((x_positions[0], y), (x_positions[-1], y), color=(0, 0, 0))
            values = [
                ["Component", "Model", "Voltage"],
                ["MCU", "STM32H743", "3.3V"],
                ["IMU", "ICM-42688-P", "3.3V"],
                ["Bus", "CAN-FD", "5V"],
            ]
            for row, cells in enumerate(values):
                for column, value in enumerate(cells):
                    page.insert_text((x_positions[column] + 8, y_positions[row] + 30), value, fontsize=9)
        if page_number == 5:
            # Sparse/merged-looking table: local rules intentionally keep it as a crop.
            for x in [80, 260, 520]:
                page.draw_line((x, 180), (x, 430), color=(0, 0, 0), width=1.5)
            for y in [180, 240, 300, 365, 430]:
                page.draw_line((80, y), (520, y), color=(0, 0, 0), width=1.5)
            page.insert_text((95, 220), "Merged multi-level result table", fontsize=11)
            page.insert_text((275, 285), "Curve and formula region", fontsize=10)
            page.draw_circle((400, 350), 40, color=(0.8, 0.1, 0.1), fill=(1, 0.85, 0.85))
        if page_number == 6:
            page.insert_text((80, 165), "SYSTEM ARCHITECTURE DIAGRAM", fontsize=12)
            boxes = [fitz.Rect(90 + index * 105, 220, 175 + index * 105, 300) for index in range(4)]
            for index, box in enumerate(boxes):
                page.draw_rect(box, color=(0.1, 0.2, 0.7), fill=(0.9, 0.95, 1))
                page.insert_text((box.x0 + 10, box.y0 + 45), f"NODE{index + 1}", fontsize=9)
                if index:
                    page.draw_line((boxes[index - 1].x1, 260), (box.x0, 260), color=(0, 0, 0), width=2)
    document.save(fixture / "source.pdf")
    document.close()
    asset.unlink(missing_ok=True)
    scan.unlink(missing_ok=True)


if __name__ == "__main__":
    generate_long_text()
    generate_long_mixed()


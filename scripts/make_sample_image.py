
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def render_key_image(path: str) -> None:
    lines = [
        "Quick Answer Key",
        "Day 01",
        "1. 3   2. 4   3. 1   4. 5   5. 2",
        "6. 4   7. 3   8. 2   9. 5  10. 1",
        "Day 02",
        "1. 2   2. 3   3. 4   4. 1   5. 5",
        "6. 1   7. 2   8. 5   9. 3  10. 4",
    ]
    font = None
    for fp in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(fp, 42)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    img = Image.new("RGB", (1100, 640), "white")
    draw = ImageDraw.Draw(img)
    y = 30
    for line in lines:
        draw.text((60, y), line, fill="black", font=font)
        y += 70
    img.save(path, "PNG")


if __name__ == "__main__":
    import sys

    render_key_image(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/key.png")

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


OUT = Path(__file__).resolve().parent / "pdf_reader.ico"
size = 512
image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)

draw.rounded_rectangle(
    (62, 34, 450, 478),
    radius=54,
    fill=(34, 42, 52, 255),
    outline=(95, 174, 235, 255),
    width=14,
)
draw.polygon(
    [(330, 34), (450, 154), (330, 154)],
    fill=(77, 107, 132, 255),
)
draw.rounded_rectangle(
    (108, 214, 404, 402),
    radius=24,
    fill=(89, 168, 232, 255),
)

font_path = Path("C:/Windows/Fonts/arialbd.ttf")
font = ImageFont.truetype(str(font_path), 104)
text = "PDF"
box = draw.textbbox((0, 0), text, font=font)
draw.text(
    ((size - (box[2] - box[0])) / 2, 244),
    text,
    font=font,
    fill=(14, 24, 34, 255),
)

image.save(
    OUT,
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print(OUT)

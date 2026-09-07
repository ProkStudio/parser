"""Native, code-generated black/gold app monogram; no external assets."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

image = Image.new("RGBA", (256, 256), "#0c0b09")
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((10, 10, 246, 246), radius=44, outline="#dfbd72", width=8)
font_path = Path("C:/Windows/Fonts/consola.ttf")
font = ImageFont.truetype(str(font_path), 160) if font_path.exists() else ImageFont.load_default(size=132)
draw.text((32, 27), "p_", font=font, fill="#dfbd72")
Path("tools").mkdir(exist_ok=True)
image.save("tools/parser.ico", sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])

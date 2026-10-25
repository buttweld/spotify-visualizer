"""Photos and QR instructions rendered as DRM-compatible BGRA frames."""
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH, HEIGHT = 1920, 1080
RESAMPLING = getattr(Image, "Resampling", Image)
from .api import DemoError

def font(size):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()

def bgra(image):
    rgb = np.asarray(image.convert("RGB"))
    frame = np.empty((image.height, image.width, 4), dtype=np.uint8)
    frame[:, :, :3] = rgb[:, :, ::-1]
    frame[:, :, 3] = 255
    return frame

def instruction_frame(title, lines, qr_url=None, size=(WIDTH, HEIGHT),
                      footer="1920 × 1080   •   1 second per photo   •   Ctrl+C to exit",
                      subtitle="GOOGLE PHOTOS  /  HDMI DEMO"):
    image = Image.new("RGB", (WIDTH, HEIGHT), "#0c1422")
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), subtitle, font=font(24), fill="#9eb6d4")
    draw.text((100, 195), title, font=font(48), fill="white")
    for index, line in enumerate(lines):
        draw.text((100, 320 + index * 62), line, font=font(27), fill="#d8e4f2")
    if qr_url:
        import qrcode
        qr = qrcode.QRCode(border=4, box_size=10)
        qr.add_data(qr_url)
        qr.make(fit=True)
        code = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        # Integer-sized modules and the quiet zone keep the code scannable on a TV.
        scale = max(1, 650 // code.width)
        if scale > 1:
            code = code.resize((code.width * scale, code.height * scale), RESAMPLING.NEAREST)
        if code.width > 750:
            raise DemoError("Selection QR code is too large for the display.")
        x, y = 1800 - code.width, (HEIGHT - code.height) // 2
        image.paste(code, (x, y))
    draw.text((100, 950), footer,
              font=font(23), fill="#9eb6d4")
    if size != (WIDTH, HEIGHT):
        image = image.resize(size, RESAMPLING.LANCZOS)
    return bgra(image)

def photo_frame(path, size=(WIDTH, HEIGHT)):
    with Image.open(path) as source:
        photo = ImageOps.exif_transpose(source).convert("RGB")
        photo = ImageOps.contain(photo, size, RESAMPLING.LANCZOS)
        image = Image.new("RGB", size, "black")
        image.paste(photo, ((size[0] - photo.width) // 2, (size[1] - photo.height) // 2))
    return bgra(image)

"""Encode screenshots at full resolution, choosing the smaller JPEG or PNG."""
from io import BytesIO

DEFAULT_JPEG_QUALITY = 80


def image_type(image):
    if image.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', 'png'
    if len(image) >= 4 and image.startswith(b'\xff\xd8\xff') and image.endswith(b'\xff\xd9'):
        return 'image/jpeg', 'jpg'
    raise ValueError('Screenshot must be JPEG or PNG')


def encode_screenshot(rgb, size, quality=DEFAULT_JPEG_QUALITY):
    from PIL import Image
    width, height = size
    if width < 1 or height < 1 or width > 8192 or height > 8192 or width * height > 16_000_000:
        raise ValueError('Screenshot exceeds pixel limit')
    if isinstance(quality, bool) or not isinstance(quality, int) or not 40 <= quality <= 95:
        quality = DEFAULT_JPEG_QUALITY
    image = Image.frombytes('RGB', size, rgb)
    jpeg = BytesIO()
    # Keep full color resolution around UI text. Streaming output avoids Pillow
    # optimized-encoder buffer failures on detailed/high-entropy screenshots.
    image.save(jpeg, format='JPEG', quality=quality, optimize=False, subsampling=0)
    png = BytesIO()
    image.save(png, format='PNG')
    jpeg_bytes, png_bytes = jpeg.getvalue(), png.getvalue()
    return jpeg_bytes if len(jpeg_bytes) < len(png_bytes) else png_bytes

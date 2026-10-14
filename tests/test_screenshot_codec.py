from io import BytesIO
import random
import unittest
from PIL import Image
from screenshot_codec import encode_screenshot, image_type


class ScreenshotCodecTests(unittest.TestCase):
    def test_photo_like_pixels_choose_smaller_jpeg_at_original_dimensions(self):
        size = (320, 180)
        rng = random.Random(17)
        rgb = bytes(rng.randrange(256) for _ in range(size[0]*size[1]*3))
        encoded = encode_screenshot(rgb, size)
        self.assertEqual(image_type(encoded), ('image/jpeg', 'jpg'))
        with Image.open(BytesIO(encoded)) as image:
            image.load()
            self.assertEqual(image.size, size)
            self.assertEqual(image.mode, 'RGB')
        png = BytesIO()
        Image.frombytes('RGB', size, rgb).save(png, format='PNG')
        self.assertLess(len(encoded), len(png.getvalue()))

    def test_flat_screen_keeps_lossless_png_when_it_is_smaller(self):
        size = (320, 180)
        rgb = bytes([30, 90, 150]) * (size[0]*size[1])
        encoded = encode_screenshot(rgb, size)
        self.assertEqual(image_type(encoded), ('image/png', 'png'))
        with Image.open(BytesIO(encoded)) as image:
            self.assertEqual(image.tobytes(), rgb)

    def test_invalid_quality_falls_back_to_default_and_excessive_pixels_are_rejected(self):
        size = (40, 40)
        rgb = random.Random(17).randbytes(size[0]*size[1]*3)
        default = encode_screenshot(rgb, size)
        for quality in [True, '80', 0, 100, None]:
            self.assertEqual(encode_screenshot(rgb, size, quality), default)
        with self.assertRaises(ValueError): encode_screenshot(b'', (8193, 1))

    def test_quality_setting_changes_jpeg_size_and_invalid_signatures_are_rejected(self):
        size = (100, 100)
        rgb = random.Random(17).randbytes(size[0]*size[1]*3)
        self.assertLess(len(encode_screenshot(rgb, size, 40)), len(encode_screenshot(rgb, size, 95)))
        for data in [b'not-image', b'\xff\xd8\xffmissing-end']:
            with self.assertRaises(ValueError): image_type(data)

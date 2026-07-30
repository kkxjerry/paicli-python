from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from paicli.images import ImageProcessor, multimodal_user_message


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class ImagesTest(unittest.TestCase):
    def test_parses_prompt_image_and_builds_multimodal_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "pixel.png").write_bytes(ONE_PIXEL_PNG)
            processor = ImageProcessor(directory)

            prompt, images = processor.from_prompt(
                "Describe this @image:pixel.png"
            )
            message = multimodal_user_message(prompt, images)

            self.assertEqual("Describe this", prompt)
            self.assertEqual((1, 1), (images[0].width, images[0].height))
            self.assertEqual("image_url", message["content"][1]["type"])

    def test_rejects_non_image_and_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "fake.png").write_text("not an image", encoding="utf-8")
            processor = ImageProcessor(directory)

            with self.assertRaises(ValueError):
                processor.load("fake.png")
            with self.assertRaises(ValueError):
                processor.load("../outside.png")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from paicli.images import ImageProcessor, multimodal_user_message


# 内嵌真实的 1x1 PNG，测试无需网络或 Pillow。
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class ImagesTest(unittest.TestCase):
    def test_parses_prompt_image_and_builds_multimodal_message(self) -> None:
        """@image 标记被移出文本，图片尺寸正确，消息含 image_url part。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：写入 1x1 PNG 并创建受项目根目录约束的处理器。
            Path(directory, "pixel.png").write_bytes(ONE_PIXEL_PNG)
            processor = ImageProcessor(directory)

            # Act：先解析 prompt，再组装模型 user message。
            prompt, images = processor.from_prompt(
                "Describe this @image:pixel.png"
            )
            message = multimodal_user_message(prompt, images)

            # Assert：同时验证文本清理、PNG 头解析和多模态结构。
            self.assertEqual("Describe this", prompt)
            self.assertEqual((1, 1), (images[0].width, images[0].height))
            self.assertEqual("image_url", message["content"][1]["type"])

    def test_rejects_non_image_and_path_escape(self) -> None:
        """只改文件扩展名不算图片，且不能读项目外路径。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：文件名是 .png，内容却不含 PNG 签名。
            Path(directory, "fake.png").write_text("not an image", encoding="utf-8")
            processor = ImageProcessor(directory)

            # Act + Assert：分别验证格式校验和路径边界。
            with self.assertRaises(ValueError):
                processor.load("fake.png")
            with self.assertRaises(ValueError):
                processor.load("../outside.png")


if __name__ == "__main__":
    unittest.main()

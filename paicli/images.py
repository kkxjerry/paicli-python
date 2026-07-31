"""Phase 21：经验证的本地图片引用与多模态用户消息。

用户可在 prompt 中写 ``@image:path.png`` 或 ``@image:"path with spaces.png"``。
系统会解析引用 -> 校验路径/大小/文件签名 -> 编码为 data URL -> 组装多模态消息。
本期不进行图片缩放、压缩、EXIF 处理或 OCR，JPEG/WebP 也未解析尺寸。
"""

from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ImageReference:
    """一个 prompt 中的原始 @image 片段及其解析后路径。"""

    raw: str
    path: str


class ImageReferenceParser:
    """从文本中查找图片引用，并可将这些标记从 prompt 中移除。"""

    # 第一个分组匹配引号路径，第二个分组匹配无空格路径。
    PATTERN = re.compile(r"@image:(?:\"([^\"]+)\"|([^\s]+))")

    @classmethod
    def parse(cls, text: str) -> list[ImageReference]:
        # finditer 保留引用在 prompt 中的原始顺序。
        return [
            ImageReference(match.group(0), match.group(1) or match.group(2))
            for match in cls.PATTERN.finditer(text)
        ]

    @classmethod
    def strip(cls, text: str) -> str:
        # 删除引用后把多个空白折叠为一个空格，避免留下大段空洞。
        return re.sub(r"\s+", " ", cls.PATTERN.sub("", text)).strip()


@dataclass(frozen=True)
class ImageAttachment:
    """已验证且可直接发给模型的图片附件。"""

    path: str
    media_type: str
    data_url: str
    width: int | None = None
    height: int | None = None


class ImageProcessor:
    """在项目根目录内安全读取图片，并转为 Base64 data URL。"""

    # 只相信文件头魔数，不相信可伪造的 .png/.jpg 扩展名。
    SIGNATURES = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFF", "image/webp"),
    )

    def __init__(
        self,
        project_root: str | Path,
        *,
        max_bytes: int = 10_000_000,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.max_bytes = max_bytes

    def load(self, raw_path: str) -> ImageAttachment:
        """校验并读取一张本地图片。"""

        # resolve + is_relative_to 防止 ../ 或符号链接读取项目外文件。
        path = (self.project_root / raw_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("image path escapes project root")
        if not path.is_file():
            raise ValueError(f"image does not exist: {raw_path}")
        data = path.read_bytes()
        # 在 Base64 编码前限制原图大小；编码后体积通常还会增加约 1/3。
        if len(data) > self.max_bytes:
            raise ValueError("image exceeds size limit")
        media_type = self._detect_type(data)
        width, height = self._dimensions(data, media_type)
        encoded = base64.b64encode(data).decode("ascii")
        return ImageAttachment(
            raw_path,
            media_type,
            f"data:{media_type};base64,{encoded}",
            width,
            height,
        )

    def from_prompt(self, prompt: str) -> tuple[str, list[ImageAttachment]]:
        """一次完成“清理文本 + 加载所有附件”。"""

        references = ImageReferenceParser.parse(prompt)
        return (
            ImageReferenceParser.strip(prompt),
            [self.load(reference.path) for reference in references],
        )

    @classmethod
    def _detect_type(cls, data: bytes) -> str:
        """通过文件签名识别 PNG/JPEG/GIF/WebP，其他格式拒绝。"""

        for signature, media_type in cls.SIGNATURES:
            if data.startswith(signature):
                # WebP 与其他 RIFF 容器共用前四字节，还必须检查 WEBP 标识。
                if media_type == "image/webp" and data[8:12] != b"WEBP":
                    continue
                return media_type
        raise ValueError("unsupported or invalid image format")

    @staticmethod
    def _dimensions(
        data: bytes,
        media_type: str,
    ) -> tuple[int | None, int | None]:
        """用固定文件头位置读取 PNG/GIF 宽高，其他格式返回未知。"""

        # PNG 的 IHDR 宽高是大端 32 位整数。
        if media_type == "image/png" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        # GIF 逻辑屏幕宽高是小端 16 位整数。
        if media_type == "image/gif" and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        return None, None


def multimodal_user_message(
    text: str,
    images: Iterable[ImageAttachment],
) -> dict[str, Any]:
    """按 OpenAI 风格的 content parts 结构组装 user 消息。"""

    attachments = list(images)
    if not attachments:
        # 无图时保持与旧版本相同的纯字符串 content，避免破坏兼容性。
        return {"role": "user", "content": text}
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    # 文本始终是第一个 part，图片按 prompt 中出现顺序追加。
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": image.data_url},
        }
        for image in attachments
    )
    return {"role": "user", "content": content}

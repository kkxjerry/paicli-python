"""Phase 21: validated image references and multimodal messages."""

from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ImageReference:
    raw: str
    path: str


class ImageReferenceParser:
    PATTERN = re.compile(r"@image:(?:\"([^\"]+)\"|([^\s]+))")

    @classmethod
    def parse(cls, text: str) -> list[ImageReference]:
        return [
            ImageReference(match.group(0), match.group(1) or match.group(2))
            for match in cls.PATTERN.finditer(text)
        ]

    @classmethod
    def strip(cls, text: str) -> str:
        return re.sub(r"\s+", " ", cls.PATTERN.sub("", text)).strip()


@dataclass(frozen=True)
class ImageAttachment:
    path: str
    media_type: str
    data_url: str
    width: int | None = None
    height: int | None = None


class ImageProcessor:
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
        path = (self.project_root / raw_path).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("image path escapes project root")
        if not path.is_file():
            raise ValueError(f"image does not exist: {raw_path}")
        data = path.read_bytes()
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
        references = ImageReferenceParser.parse(prompt)
        return (
            ImageReferenceParser.strip(prompt),
            [self.load(reference.path) for reference in references],
        )

    @classmethod
    def _detect_type(cls, data: bytes) -> str:
        for signature, media_type in cls.SIGNATURES:
            if data.startswith(signature):
                if media_type == "image/webp" and data[8:12] != b"WEBP":
                    continue
                return media_type
        raise ValueError("unsupported or invalid image format")

    @staticmethod
    def _dimensions(
        data: bytes,
        media_type: str,
    ) -> tuple[int | None, int | None]:
        if media_type == "image/png" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if media_type == "image/gif" and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        return None, None


def multimodal_user_message(
    text: str,
    images: Iterable[ImageAttachment],
) -> dict[str, Any]:
    attachments = list(images)
    if not attachments:
        return {"role": "user", "content": text}
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": image.data_url},
        }
        for image in attachments
    )
    return {"role": "user", "content": content}

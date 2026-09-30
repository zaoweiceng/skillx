"""User image attachments: bounded binary storage, separate from text prompts."""

import base64
import binascii
import os
from pathlib import Path
from uuid import UUID, uuid4

from app.config import get_settings

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def decode_image(data: str, mime_type: str) -> bytes:
    try:
        binary = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("图片数据不是有效的 Base64") from exc
    signatures = {
        "image/png": binary.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": binary.startswith(b"\xff\xd8\xff"),
        "image/gif": binary.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": binary.startswith(b"RIFF") and binary[8:12] == b"WEBP",
    }
    if not signatures.get(mime_type):
        raise ValueError("图片内容与声明格式不符，仅支持 PNG、JPEG、WebP 和 GIF")
    if len(binary) > MAX_IMAGE_BYTES:
        raise ValueError("单张图片不能超过 5 MB")
    return binary


def image_path(reference: dict) -> Path:
    image_id = str(UUID(reference["id"]))
    return (
        get_settings().artifact_root
        / "chat-images"
        / (image_id + IMAGE_EXTENSIONS[reference["mimeType"]])
    )


def store_images(images, message_id: str) -> list[dict]:
    references = []
    for image in images:
        reference = {"id": str(uuid4()), "name": image.name, "mimeType": image.mime_type}
        binary = decode_image(image.data, image.mime_type)
        path = image_path(reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(binary)
        references.append(
            {
                **reference,
                "size": len(binary),
                "url": f"/chat-images/{message_id}/{reference['id']}",
            }
        )
    return references


def agent_images(references: list[dict]) -> list[dict[str, str]]:
    return [
        {"path": str(image_path(item).resolve()), "mimeType": item["mimeType"]}
        for item in references
    ]

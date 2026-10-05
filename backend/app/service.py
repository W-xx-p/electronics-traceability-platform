from __future__ import annotations

import io
import time
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .db import utc_now
from .ocr import OCRProvider
from .repository import Repository
from .rules import evaluate_inspection
from .storage import LocalEvidenceStorage


ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
CONTENT_FORMATS = {
    "image/png": {"PNG"},
    "image/jpeg": {"JPEG"},
    "image/webp": {"WEBP"},
}


class InspectionService:
    def __init__(self, repository: Repository, ocr: OCRProvider, upload_dir: Path):
        self.repository = repository
        self.ocr = ocr
        self.upload_dir = Path(upload_dir)
        self.storage = LocalEvidenceStorage(self.upload_dir)

    def inspect(
        self,
        *,
        order_id: int,
        image_bytes: bytes,
        filename: str,
        content_type: str,
        idempotency_key: str | None,
        workstation_id: int | None = None,
    ) -> dict:
        started = time.perf_counter()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError("仅支持 PNG、JPEG 和 WebP 图片")
        if not image_bytes:
            raise ValueError("上传文件为空")
        if len(image_bytes) > MAX_FILE_SIZE:
            raise ValueError("图片不能超过 10 MiB")
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                if image.format not in CONTENT_FORMATS[content_type]:
                    raise ValueError("文件内容与声明的图片类型不一致")
                width, height = image.size
                if width < 64 or height < 32:
                    raise ValueError("图片分辨率过低，无法可靠识别")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片像素数量超过 4000 万限制")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("不支持动画或多帧图片")
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise ValueError("文件不是有效或完整的图片") from exc
        if idempotency_key:
            existing = self.repository.get_by_idempotency_key(idempotency_key)
            if existing:
                return existing

        order = self.repository.get_order(order_id)
        if not order:
            raise LookupError("生产订单不存在")
        if order["status"] != "active":
            raise ValueError("只有 active 状态的生产订单可以执行质检")
        if workstation_id is not None:
            workstation = self.repository.get_workstation(workstation_id)
            if not workstation or not workstation["active"]:
                raise LookupError("质检工位不存在或已停用")
            self.repository.heartbeat_workstation(workstation_id)

        recognized = self.ocr.recognize(image_bytes)
        decision = evaluate_inspection(
            result=recognized,
            rules=order["rules"],
            expected_model=order["product_code"],
            expected_batch=order["batch_number"],
            is_duplicate_serial=self.repository.serial_exists,
        )
        inspection_id = str(uuid.uuid4())
        evidence = self.storage.put(image_bytes, filename)

        def field(name: str) -> str | None:
            value = recognized.fields.get(name, {}).get("value")
            return str(value) if value is not None else None

        record = {
            "id": inspection_id,
            "order_id": order_id,
            "product_model_id": order["product_model_id"],
            "serial_number": field("serial_number"),
            "batch_number": field("batch_number"),
            "recognized_model": field("product_model"),
            "status": decision.status,
            "overall_confidence": decision.overall_confidence,
            "image_path": evidence.path,
            "image_sha256": evidence.sha256,
            "fields": recognized.fields,
            "qr_payload": recognized.qr_payload,
            "reasons": decision.reasons,
            "quality": recognized.quality,
            "idempotency_key": idempotency_key,
            "created_at": utc_now(),
            "workstation_id": workstation_id,
            "processing_ms": round((time.perf_counter() - started) * 1000),
        }
        try:
            self.repository.insert_inspection(record)
        except Exception:
            self.storage.delete(evidence.path)
            raise
        return self.repository.get_inspection(inspection_id) or record

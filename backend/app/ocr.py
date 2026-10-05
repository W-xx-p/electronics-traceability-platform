from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image


@dataclass
class OCRResult:
    fields: dict[str, dict[str, Any]]
    qr_payload: dict[str, str]
    quality: dict[str, Any]
    raw_text: str


class OCRProvider(Protocol):
    def recognize(self, image_bytes: bytes) -> OCRResult: ...


class DemoMetadataOCRProvider:
    """Reads deterministic metadata embedded in generated demo PNG labels.

    This provider exists so the full manufacturing workflow can run without a
    heavyweight model download. It is deliberately explicit and must never be
    presented as real OCR performance.
    """

    def recognize(self, image_bytes: bytes) -> OCRResult:
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                payload = image.info.get("electrotrace")
        except Exception as exc:
            raise ValueError("无法读取图片，请上传有效的 PNG/JPEG 文件") from exc

        if not payload:
            raise ValueError(
                "演示 OCR 只能读取项目生成的样例标签；真实图片请配置 ELECTROTRACE_OCR_PROVIDER=paddle"
            )

        data = json.loads(payload)
        return OCRResult(
            fields=data.get("fields", {}),
            qr_payload=data.get("qr_payload", {}),
            quality=data.get("quality", {"score": 1.0, "issues": []}),
            raw_text=data.get("raw_text", ""),
        )


class PaddleOCRProvider:
    """Optional PaddleOCR adapter.

    PaddleOCR is imported lazily so the application and tests remain lightweight.
    Text is converted into manufacturing fields using configurable-looking label
    prefixes. Production deployments should replace this generic extractor with
    per-product ROI templates.
    """

    PATTERNS = {
        "serial_number": re.compile(r"(?:SN|SERIAL)\s*[:#-]?\s*([A-Z0-9-]{4,})", re.I),
        "product_model": re.compile(r"(?:MODEL|TYPE)\s*[:#-]?\s*([A-Z0-9-]{2,})", re.I),
        "batch_number": re.compile(r"(?:BATCH|LOT)\s*[:#-]?\s*([A-Z0-9-]{2,})", re.I),
        "manufacturing_date": re.compile(r"(?:DATE|MFG)\s*[:#-]?\s*([0-9./-]{6,10})", re.I),
        "rohs": re.compile(r"\b(ROHS)\b", re.I),
    }

    def __init__(self) -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                "PaddleOCR 未安装。请在兼容的 Python 3.11 环境安装 paddleocr 和 paddlepaddle。"
            ) from exc
        self.engine = PaddleOCR(use_doc_orientation_classify=True, use_doc_unwarping=True)

    def recognize(self, image_bytes: bytes) -> OCRResult:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("无法解码上传图片")
        results = list(self.engine.predict(image))
        text_items: list[tuple[str, float]] = []
        for result in results:
            payload = getattr(result, "json", result)
            self._collect_text(payload, text_items)

        raw_text = "\n".join(text for text, _ in text_items)
        mean_confidence = (
            sum(score for _, score in text_items) / len(text_items) if text_items else 0.0
        )
        fields: dict[str, dict[str, Any]] = {}
        for name, pattern in self.PATTERNS.items():
            match = pattern.search(raw_text)
            if match:
                fields[name] = {"value": match.group(1).upper(), "confidence": mean_confidence}

        detector = cv2.QRCodeDetector()
        qr_text, _, _ = detector.detectAndDecode(image)
        qr_payload = self._parse_qr_payload(qr_text)
        return OCRResult(
            fields=fields,
            qr_payload=qr_payload,
            quality=analyze_image_quality(image),
            raw_text=raw_text,
        )

    @classmethod
    def _collect_text(cls, value: Any, target: list[tuple[str, float]]) -> None:
        if isinstance(value, dict):
            texts = value.get("rec_texts")
            scores = value.get("rec_scores")
            if isinstance(texts, list):
                for index, text in enumerate(texts):
                    score = float(scores[index]) if isinstance(scores, list) and index < len(scores) else 0.0
                    target.append((str(text), score))
            for child in value.values():
                cls._collect_text(child, target)
        elif isinstance(value, list):
            for child in value:
                cls._collect_text(child, target)

    @staticmethod
    def _parse_qr_payload(value: str) -> dict[str, str]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return {str(key): str(item) for key, item in parsed.items()}
        except json.JSONDecodeError:
            pairs = [part.split("=", 1) for part in value.split(";") if "=" in part]
            return {key.strip(): item.strip() for key, item in pairs}


def build_ocr_provider(name: str) -> OCRProvider:
    if name == "demo":
        return DemoMetadataOCRProvider()
    if name == "paddle":
        return PaddleOCRProvider()
    raise ValueError(f"不支持的 OCR provider: {name}")


def analyze_image_quality(image: Any) -> dict[str, Any]:
    """Score blur, exposure and contrast with deterministic OpenCV heuristics."""
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    contrast = float(gray.std())
    issues: list[str] = []
    scores: list[float] = []

    blur_score = min(blur_variance / 180.0, 1.0)
    if blur_variance < 70:
        issues.append("blur")
    scores.append(blur_score)

    exposure_score = max(0.0, 1.0 - abs(brightness - 135.0) / 135.0)
    if brightness < 45:
        issues.append("underexposed")
    elif brightness > 225:
        issues.append("overexposed")
    scores.append(exposure_score)

    contrast_score = min(contrast / 55.0, 1.0)
    if contrast < 25:
        issues.append("low_contrast")
    scores.append(contrast_score)
    return {
        "score": round(sum(scores) / len(scores), 4),
        "issues": issues,
        "blur_variance": round(blur_variance, 2),
        "brightness": round(brightness, 2),
        "contrast": round(contrast, 2),
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
    }

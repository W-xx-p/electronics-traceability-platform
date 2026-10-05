from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "sample-data" / "images"


def field(value: str, confidence: float = 0.97) -> dict:
    return {"value": value, "confidence": confidence}


def make_label(filename: str, *, values: dict[str, str], qr_payload: dict[str, str] | None = None,
               confidence: float = 0.97, quality: float = 0.95, issues: list[str] | None = None) -> None:
    image = Image.new("RGB", (1100, 620), "#f7f5ec")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=28)
    title_font = ImageFont.load_default(size=42)

    draw.rounded_rectangle((40, 40, 1060, 580), radius=22, outline="#173f35", width=4, fill="#fffefa")
    draw.rectangle((40, 40, 1060, 125), fill="#0f6a4f")
    draw.text((75, 63), "ELECTROTRACE ELECTRONICS", fill="white", font=title_font)

    lines = [
        ("MODEL", values.get("product_model", "")),
        ("SN", values.get("serial_number", "")),
        ("BATCH", values.get("batch_number", "")),
        ("MFG DATE", values.get("manufacturing_date", "")),
        ("COMPLIANCE", values.get("rohs", "")),
    ]
    y = 165
    for label, value in lines:
        draw.text((80, y), f"{label:<12} {value}", fill="#12211d", font=font)
        y += 63

    draw.rounded_rectangle((735, 170, 1000, 435), radius=12, outline="#12211d", width=3)
    draw.text((795, 270), "QR PAYLOAD", fill="#12211d", font=font)
    draw.text((80, 520), "DEMO LABEL · metadata-backed OCR fixture", fill="#65736e", font=font)

    embedded = {
        "fields": {key: field(value, confidence) for key, value in values.items()},
        "qr_payload": qr_payload if qr_payload is not None else {
            key: value for key, value in values.items() if key in {"serial_number", "product_model", "batch_number"}
        },
        "quality": {"score": quality, "issues": issues or []},
        "raw_text": "\n".join(f"{key}: {value}" for key, value in values.items()),
    }
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("electrotrace", json.dumps(embedded, ensure_ascii=False))
    image.save(OUTPUT_DIR / filename, pnginfo=metadata)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = {
        "product_model": "ECU-A100",
        "batch_number": "B260001",
        "manufacturing_date": "2026-10-05",
        "rohs": "ROHS",
    }
    make_label("01-pass.png", values={**base, "serial_number": "A100-20260001"})
    make_label(
        "02-review-low-confidence.png",
        values={**base, "serial_number": "A100-20260002"},
        confidence=0.68,
        quality=0.58,
        issues=["blur", "low_contrast"],
    )
    make_label("03-duplicate-sn.png", values={**base, "serial_number": "A100-20260001"})
    make_label(
        "04-wrong-model.png",
        values={**base, "product_model": "ECU-B200", "serial_number": "A100-20260004"},
    )
    conflict = {**base, "serial_number": "A100-20260005"}
    make_label(
        "05-qr-conflict.png",
        values=conflict,
        qr_payload={
            "product_model": "ECU-A100",
            "batch_number": "B260001",
            "serial_number": "A100-20269999",
        },
    )
    print(f"Generated demo labels in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

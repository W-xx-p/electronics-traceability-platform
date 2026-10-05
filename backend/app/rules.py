from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from .ocr import OCRResult


@dataclass
class RuleDecision:
    status: str
    reasons: list[dict[str, str]]
    overall_confidence: float


def validate_rule_config(rules: dict[str, Any]) -> None:
    fields = rules.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("规则必须包含非空 fields 配置")
    for field_name, spec in fields.items():
        if not isinstance(field_name, str) or not isinstance(spec, dict):
            raise ValueError("字段规则结构无效")
        pattern = spec.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or len(pattern) > 256:
                raise ValueError(f"{field_name} 的正则表达式无效或过长")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"{field_name} 的正则表达式无法编译") from exc
    for key in ("min_confidence", "min_field_confidence", "min_quality"):
        try:
            value = float(rules.get(key, 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须为 0 到 1 之间的数字") from exc
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{key} 必须为 0 到 1 之间的数字")


def evaluate_inspection(
    *,
    result: OCRResult,
    rules: dict[str, Any],
    expected_model: str,
    expected_batch: str,
    is_duplicate_serial: Callable[[str], bool],
) -> RuleDecision:
    reasons: list[dict[str, str]] = []
    fields = result.fields
    confidences = [float(item.get("confidence", 0.0)) for item in fields.values()]
    overall_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    field_rules = rules.get("fields", {})
    for field_name, spec in field_rules.items():
        value = str(fields.get(field_name, {}).get("value", "")).strip()
        if spec.get("required") and not value:
            reasons.append(_reason("required_missing", "reject", f"缺少必填字段：{field_name}"))
            continue
        pattern = spec.get("pattern")
        if value and pattern and re.fullmatch(pattern, value) is None:
            reasons.append(_reason("invalid_format", "reject", f"{field_name} 格式不符合规则"))

    serial = _value(fields, "serial_number")
    if serial and field_rules.get("serial_number", {}).get("unique") and is_duplicate_serial(serial):
        reasons.append(_reason("duplicate_serial", "reject", f"序列号 {serial} 已存在"))

    recognized_model = _value(fields, "product_model")
    if rules.get("checks", {}).get("order_model_match") and recognized_model != expected_model:
        reasons.append(
            _reason(
                "order_model_mismatch",
                "reject",
                f"识别型号 {recognized_model or '空'} 与订单型号 {expected_model} 不一致",
            )
        )

    batch = _value(fields, "batch_number")
    if rules.get("checks", {}).get("order_batch_match") and batch != expected_batch:
        reasons.append(
            _reason(
                "order_batch_mismatch",
                "reject",
                f"识别批次 {batch or '空'} 与订单批次 {expected_batch} 不一致",
            )
        )

    if rules.get("checks", {}).get("ocr_qr_consistency"):
        for field_name in ("serial_number", "product_model", "batch_number"):
            qr_value = str(result.qr_payload.get(field_name, "")).strip()
            ocr_value = _value(fields, field_name)
            if qr_value and ocr_value and qr_value != ocr_value:
                reasons.append(
                    _reason(
                        "ocr_qr_mismatch",
                        "reject",
                        f"{field_name} 的 OCR 值与码值不一致",
                    )
                )

    if rules.get("checks", {}).get("qr_required") and not result.qr_payload:
        reasons.append(_reason("qr_missing", "reject", "未读取到必需的二维码内容"))

    manufacturing_date = _value(fields, "manufacturing_date")
    if manufacturing_date and rules.get("checks", {}).get("manufacturing_date_valid"):
        try:
            parsed_date = datetime.strptime(manufacturing_date, "%Y-%m-%d").date()
            if parsed_date > datetime.now(UTC).date():
                reasons.append(_reason("future_manufacturing_date", "reject", "生产日期不能晚于当前日期"))
        except ValueError:
            reasons.append(_reason("invalid_manufacturing_date", "reject", "生产日期不是有效日期"))

    if rules.get("checks", {}).get("rohs_required") and _value(fields, "rohs").upper() != "ROHS":
        reasons.append(_reason("rohs_missing", "reject", "未识别到必需的 RoHS 标识"))

    min_confidence = float(rules.get("min_confidence", 0.8))
    if overall_confidence < min_confidence:
        reasons.append(
            _reason(
                "low_confidence",
                "review",
                f"平均识别置信度 {overall_confidence:.2f} 低于阈值 {min_confidence:.2f}",
            )
        )

    min_field_confidence = float(rules.get("min_field_confidence", min_confidence))
    for field_name, item in fields.items():
        confidence = float(item.get("confidence", 0.0))
        if confidence < min_field_confidence:
            reasons.append(
                _reason(
                    "low_field_confidence", "review",
                    f"字段 {field_name} 置信度 {confidence:.2f} 低于阈值 {min_field_confidence:.2f}",
                )
            )

    quality_score = float(result.quality.get("score", 1.0))
    min_quality = float(rules.get("min_quality", 0.65))
    if quality_score < min_quality:
        reasons.append(
            _reason(
                "poor_image_quality",
                "review",
                f"图片质量分数 {quality_score:.2f} 低于阈值 {min_quality:.2f}",
            )
        )

    severities = {reason["severity"] for reason in reasons}
    if "reject" in severities:
        status = "reject"
    elif "review" in severities:
        status = "review"
    else:
        status = "pass"
        reasons.append(_reason("all_checks_passed", "info", "所有质量规则均通过"))
    return RuleDecision(status=status, reasons=reasons, overall_confidence=overall_confidence)


def _value(fields: dict[str, dict[str, Any]], name: str) -> str:
    return str(fields.get(name, {}).get("value", "")).strip()


def _reason(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}

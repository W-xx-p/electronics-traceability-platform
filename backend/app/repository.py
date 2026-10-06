from __future__ import annotations

import json
import hashlib
import random
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database, decode_json_columns, utc_now


DEFAULT_RULES: dict[str, Any] = {
    "fields": {
        "serial_number": {"required": True, "pattern": r"^A100-[0-9]{8}$", "unique": True},
        "product_model": {"required": True, "pattern": r"^ECU-A100$"},
        "batch_number": {"required": True, "pattern": r"^B[0-9]{6}$"},
        "manufacturing_date": {"required": True, "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
    },
    "checks": {
        "ocr_qr_consistency": True,
        "qr_required": True,
        "order_model_match": True,
        "order_batch_match": True,
        "rohs_required": True,
        "manufacturing_date_valid": True,
    },
    "min_confidence": 0.8,
    "min_field_confidence": 0.75,
    "min_quality": 0.65,
}


class Repository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _enqueue_outbox(
        conn: Any, aggregate_type: str, aggregate_id: str,
        event_type: str, payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """INSERT INTO outbox_events(
                   id, aggregate_type, aggregate_id, event_type, payload_json, status, created_at
               ) VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (
                str(uuid.uuid4()), aggregate_type, aggregate_id, event_type,
                json.dumps(payload, ensure_ascii=False), utc_now(),
            ),
        )

    def seed_demo(self) -> None:
        if not self.db.fetch_one("SELECT id FROM product_models LIMIT 1"):
            model_id = self.create_product_model("ECU-A100", "ECU A100 控制板", DEFAULT_RULES)
            self.create_order("PO-2026-001", model_id, "B260001", 500)
        if not self.db.fetch_one("SELECT id FROM workstations LIMIT 1"):
            self.create_workstation("LINE-A-01", "A 线视觉质检工位", "一厂 SMT A 线")

    def seed_demo_inspections(self, target_count: int) -> int:
        """Seed deterministic synthetic history for local validation environments."""
        target_count = min(max(int(target_count), 0), 50_000)
        now = datetime.now(UTC)

        def demo_timestamp(index: int) -> datetime:
            day_offset = index % 14
            target_date = now.date() - timedelta(days=13 - day_offset)
            hour_range = max(now.hour + 1, 1) if day_offset == 13 else 24
            return datetime(
                target_date.year, target_date.month, target_date.day,
                (index * 7) % hour_range, (index * 13) % 60, (index * 17) % 60,
                tzinfo=UTC,
            )

        count_row = self.db.fetch_one("SELECT COUNT(*) AS count FROM inspections") or {}
        existing = int(count_row.get("count") or 0)
        if existing >= target_count:
            synthetic = self.db.fetch_all(
                """SELECT id, serial_number FROM inspections
                   WHERE image_path = '' AND image_sha256 = '' AND serial_number LIKE 'A100-26%'"""
            )
            updates: list[tuple[str, str]] = []
            for row in synthetic:
                try:
                    index = int(row["serial_number"].split("-", 1)[1]) - 26_010_000
                except (AttributeError, ValueError):
                    continue
                if index >= 0:
                    updates.append((demo_timestamp(index).isoformat(), row["id"]))
            if updates:
                with self.db.connect() as conn:
                    conn.executemany("UPDATE inspections SET created_at = ? WHERE id = ?", updates)
            return 0

        model = self.db.fetch_one("SELECT id FROM product_models ORDER BY id LIMIT 1")
        if not model:
            self.seed_demo()
            model = self.db.fetch_one("SELECT id FROM product_models ORDER BY id LIMIT 1")
        if not model:
            raise RuntimeError("无法创建本地验证产品型号")
        model_id = int(model["id"])

        for number, batch in (("PO-2026-002", "B260002"), ("PO-2026-003", "B260003")):
            if not self.db.fetch_one("SELECT id FROM production_orders WHERE order_number = ?", (number,)):
                self.create_order(number, model_id, batch, 6000)
        for code, name, location in (
            ("LINE-A-02", "A 线终检工位", "一厂 SMT A 线"),
            ("LINE-B-01", "B 线视觉质检工位", "一厂组装 B 线"),
            ("LINE-B-02", "B 线终检工位", "一厂组装 B 线"),
        ):
            if not self.db.fetch_one("SELECT id FROM workstations WHERE code = ?", (code,)):
                self.create_workstation(code, name, location)

        orders = self.db.fetch_all(
            "SELECT id, batch_number FROM production_orders WHERE status = 'active' ORDER BY id"
        )
        stations = self.db.fetch_all("SELECT id FROM workstations WHERE active = 1 ORDER BY id")
        rng = random.Random(20261006)
        rows: list[tuple[Any, ...]] = []
        review_rows: list[tuple[str, str]] = []
        reason_catalog = {
            "review": [
                ("low_confidence", "整体识别置信度低于自动放行阈值"),
                ("poor_image_quality", "图像存在轻微反光或清晰度不足"),
            ],
            "reject": [
                ("duplicate_serial", "序列号已存在"),
                ("order_model_mismatch", "识别型号与生产订单不一致"),
                ("qr_ocr_conflict", "二维码内容与 OCR 结果不一致"),
                ("invalid_format", "关键字段格式不符合产品规则"),
            ],
        }

        for index in range(existing, target_count):
            roll = (index * 73 + 19) % 1000
            status = "pass" if roll < 967 else "review" if roll < 986 else "reject"
            created = demo_timestamp(index)
            order = orders[index % len(orders)]
            station = stations[index % len(stations)]
            serial = f"A100-{26_010_000 + index:08d}"
            if status == "pass":
                confidence = round(0.955 + rng.random() * 0.04, 4)
                reasons = [{"code": "all_checks_passed", "message": "所有质量规则均通过", "severity": "info"}]
            else:
                confidence = round((0.72 if status == "review" else 0.86) + rng.random() * 0.09, 4)
                code, message = reason_catalog[status][index % len(reason_catalog[status])]
                reasons = [{
                    "code": code,
                    "message": message,
                    "severity": "warning" if status == "review" else "error",
                }]
            fields = {
                "serial_number": {"value": serial, "confidence": confidence},
                "product_model": {"value": "ECU-A100", "confidence": min(confidence + 0.01, 0.999)},
                "batch_number": {"value": order["batch_number"], "confidence": min(confidence + 0.015, 0.999)},
                "manufacturing_date": {"value": created.date().isoformat(), "confidence": confidence},
                "rohs": {"value": "ROHS", "confidence": min(confidence + 0.02, 0.999)},
            }
            inspection_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"electrotrace-demo-{index}"))
            rows.append((
                inspection_id, order["id"], model_id, serial, order["batch_number"], "ECU-A100",
                status, confidence, "", json.dumps(fields, ensure_ascii=False),
                json.dumps({
                    "serial_number": serial,
                    "product_model": "ECU-A100",
                    "batch_number": order["batch_number"],
                }, ensure_ascii=False),
                json.dumps(reasons, ensure_ascii=False),
                json.dumps({"score": round(0.82 + rng.random() * 0.17, 4), "issues": []}, ensure_ascii=False),
                None, created.isoformat(), station["id"], 72 + (index * 37) % 210, "",
            ))
            if status == "review":
                review_rows.append((inspection_id, created.isoformat()))

        with self.db.connect() as conn:
            conn.executemany(
                """INSERT INTO inspections(
                       id, order_id, product_model_id, serial_number, batch_number, recognized_model,
                       status, overall_confidence, image_path, fields_json, qr_payload_json,
                       reasons_json, quality_json, idempotency_key, created_at, workstation_id,
                       processing_ms, image_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.executemany(
                "INSERT INTO review_tasks(inspection_id, status, created_at) VALUES (?, 'open', ?)",
                review_rows,
            )
        self.db.audit(
            "demo_dataset", "synthetic", "seeded",
            {"records_added": len(rows), "target_count": target_count},
        )
        return len(rows)

    def create_product_model(self, code: str, name: str, rules: dict[str, Any]) -> int:
        model_id = self.db.execute(
            "INSERT INTO product_models(code, name, rules_json, created_at) VALUES (?, ?, ?, ?)",
            (code, name, json.dumps(rules, ensure_ascii=False), utc_now()),
        )
        self.db.audit("product_model", str(model_id), "created", {"code": code})
        self.db.execute(
            """INSERT INTO product_rule_versions(
                   product_model_id, version, rules_json, change_reason, created_by, created_at
               ) VALUES (?, 1, ?, 'initial configuration', 'system', ?)""",
            (model_id, json.dumps(rules, ensure_ascii=False), utc_now()),
        )
        return model_id

    def update_product_rules(
        self, model_id: int, rules: dict[str, Any], reason: str, actor: str
    ) -> dict[str, Any] | None:
        if not self.get_product_model(model_id):
            return None
        latest = self.db.fetch_one(
            "SELECT MAX(version) AS version FROM product_rule_versions WHERE product_model_id = ?",
            (model_id,),
        )
        version = int((latest or {}).get("version") or 0) + 1
        payload = json.dumps(rules, ensure_ascii=False)
        with self.db.connect() as conn:
            conn.execute("UPDATE product_models SET rules_json = ? WHERE id = ?", (payload, model_id))
            conn.execute(
                """INSERT INTO product_rule_versions(
                       product_model_id, version, rules_json, change_reason, created_by, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (model_id, version, payload, reason, actor, utc_now()),
            )
        self.db.audit("product_model", str(model_id), "rules_updated", {"version": version, "actor": actor, "reason": reason})
        return self.get_product_model(model_id)

    def list_rule_versions(self, model_id: int) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT * FROM product_rule_versions WHERE product_model_id = ? ORDER BY version DESC",
            (model_id,),
        )
        return [decode_json_columns(row, ("rules_json",)) for row in rows]  # type: ignore[list-item]

    def list_product_models(self) -> list[dict[str, Any]]:
        rows = self.db.fetch_all("SELECT * FROM product_models ORDER BY id")
        return [decode_json_columns(row, ("rules_json",)) for row in rows]  # type: ignore[list-item]

    def get_product_model(self, model_id: int) -> dict[str, Any] | None:
        return decode_json_columns(
            self.db.fetch_one("SELECT * FROM product_models WHERE id = ?", (model_id,)),
            ("rules_json",),
        )

    def create_order(self, order_number: str, product_model_id: int, batch_number: str, target: int) -> int:
        order_id = self.db.execute(
            """INSERT INTO production_orders(
                   order_number, product_model_id, batch_number, target_quantity, status, created_at
               ) VALUES (?, ?, ?, ?, 'active', ?)""",
            (order_number, product_model_id, batch_number, target, utc_now()),
        )
        self.db.audit("production_order", str(order_id), "created", {"order_number": order_number})
        return order_id

    def list_orders(self) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """SELECT o.*, p.code AS product_code, p.name AS product_name
               FROM production_orders o
               JOIN product_models p ON p.id = o.product_model_id
               ORDER BY o.id"""
        )

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            """SELECT o.*, p.code AS product_code, p.name AS product_name, p.rules_json
               FROM production_orders o
               JOIN product_models p ON p.id = o.product_model_id
               WHERE o.id = ?""",
            (order_id,),
        )
        return decode_json_columns(row, ("rules_json",))

    def update_order_status(self, order_id: int, status: str, actor: str) -> dict[str, Any] | None:
        order = self.get_order(order_id)
        if not order:
            return None
        current = order["status"]
        allowed = {
            "active": {"completed", "cancelled"},
            "completed": {"active"},
            "cancelled": set(),
        }
        if status != current and status not in allowed.get(current, set()):
            raise ValueError(f"订单不能从 {current} 变更为 {status}")
        with self.db.connect() as conn:
            conn.execute("UPDATE production_orders SET status = ? WHERE id = ?", (status, order_id))
            self._enqueue_outbox(
                conn, "production_order", str(order_id), "production_order.status_changed",
                {"from": current, "to": status},
            )
        self.db.audit("production_order", str(order_id), "status_changed", {"from": current, "to": status, "actor": actor})
        return self.get_order(order_id)

    def serial_exists(self, serial: str) -> bool:
        return self.db.fetch_one(
            "SELECT id FROM inspections WHERE serial_number = ? AND status != 'void' LIMIT 1",
            (serial,),
        ) is not None

    def insert_inspection(self, record: dict[str, Any]) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO inspections(
                   id, order_id, product_model_id, serial_number, batch_number,
                   recognized_model, status, overall_confidence, image_path,
                   fields_json, qr_payload_json, reasons_json, quality_json,
                   idempotency_key, created_at, workstation_id, processing_ms, image_sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"], record["order_id"], record["product_model_id"],
                    record.get("serial_number"), record.get("batch_number"),
                    record.get("recognized_model"), record["status"],
                    record["overall_confidence"], record["image_path"],
                    json.dumps(record["fields"], ensure_ascii=False),
                    json.dumps(record["qr_payload"], ensure_ascii=False),
                    json.dumps(record["reasons"], ensure_ascii=False),
                    json.dumps(record["quality"], ensure_ascii=False),
                    record.get("idempotency_key"), record["created_at"],
                    record.get("workstation_id"), record.get("processing_ms", 0),
                    record.get("image_sha256", ""),
                ),
            )
            if record["status"] == "review":
                conn.execute(
                    "INSERT INTO review_tasks(inspection_id, status, created_at) VALUES (?, 'open', ?)",
                    (record["id"], utc_now()),
                )
            self._enqueue_outbox(
                conn, "inspection", record["id"], "inspection.completed",
                {"status": record["status"], "serial_number": record.get("serial_number"), "order_id": record["order_id"]},
            )
        self.db.audit("inspection", record["id"], "created", {"status": record["status"]})

    def get_inspection(self, inspection_id: str) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            """SELECT i.*, o.order_number, p.code AS product_code
               FROM inspections i
               JOIN production_orders o ON o.id = i.order_id
               JOIN product_models p ON p.id = i.product_model_id
               WHERE i.id = ?""",
            (inspection_id,),
        )
        result = decode_json_columns(
            row, ("fields_json", "qr_payload_json", "reasons_json", "quality_json")
        )
        if result:
            result.pop("image_path", None)
        return result

    def get_evidence(self, inspection_id: str) -> dict[str, Any] | None:
        return self.db.fetch_one(
            "SELECT id, serial_number, image_path, image_sha256 FROM inspections WHERE id = ?",
            (inspection_id,),
        )

    def get_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        row = self.db.fetch_one("SELECT id FROM inspections WHERE idempotency_key = ?", (key,))
        return self.get_inspection(row["id"]) if row else None

    def trace_serial(self, serial: str) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """SELECT i.id FROM inspections i
               WHERE i.serial_number = ? ORDER BY i.created_at DESC""",
            (serial,),
        )
        return [item for row in rows if (item := self.get_inspection(row["id"]))]

    def list_reviews(self, status: str = "open") -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """SELECT r.*, i.serial_number, i.status AS inspection_status,
                      i.overall_confidence, i.reasons_json, i.fields_json,
                      o.order_number, p.code AS product_code
               FROM review_tasks r
               JOIN inspections i ON i.id = r.inspection_id
               JOIN production_orders o ON o.id = i.order_id
               JOIN product_models p ON p.id = i.product_model_id
               WHERE r.status = ? ORDER BY r.created_at""",
            (status,),
        )
        return [
            decode_json_columns(row, ("reasons_json", "fields_json"))  # type: ignore[arg-type]
            for row in rows
        ]

    def resolve_review(
        self,
        review_id: int,
        resolution: str,
        reviewer: str,
        reason: str,
        corrected_fields: dict[str, str],
    ) -> dict[str, Any] | None:
        review = self.db.fetch_one("SELECT * FROM review_tasks WHERE id = ?", (review_id,))
        if not review or review["status"] != "open":
            return None
        now = utc_now()
        with self.db.connect() as conn:
            cursor = conn.execute(
                """UPDATE review_tasks SET status='resolved', resolution=?, reviewer=?, reason=?,
                          corrected_fields_json=?, resolved_at=?, version=version+1
                   WHERE id=? AND status='open' AND (claimed_by IS NULL OR claimed_by=?)""",
                (resolution, reviewer, reason, json.dumps(corrected_fields, ensure_ascii=False), now, review_id, reviewer),
            )
            if cursor.rowcount != 1:
                return None
            conn.execute(
                "UPDATE inspections SET status=?, version=version+1 WHERE id=?",
                (resolution, review["inspection_id"]),
            )
            self._enqueue_outbox(
                conn, "inspection", review["inspection_id"], "inspection.review_resolved",
                {"resolution": resolution, "reviewer": reviewer},
            )
        self.db.audit(
            "review", str(review_id), "resolved",
            {"resolution": resolution, "reviewer": reviewer, "corrected_fields": corrected_fields},
        )
        return self.get_inspection(review["inspection_id"])

    def claim_review(self, review_id: int, reviewer: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            cursor = conn.execute(
                """UPDATE review_tasks SET claimed_by=?, claimed_at=?, version=version+1
                   WHERE id=? AND status='open' AND (claimed_by IS NULL OR claimed_by=?)""",
                (reviewer, utc_now(), review_id, reviewer),
            )
            if cursor.rowcount != 1:
                return None
        self.db.audit("review", str(review_id), "claimed", {"reviewer": reviewer})
        rows = self.list_reviews("open")
        return next((item for item in rows if item["id"] == review_id), None)

    def dashboard(self) -> dict[str, Any]:
        totals = self.db.fetch_one(
            """SELECT COUNT(*) AS total,
                      SUM(status='pass') AS passed,
                      SUM(status='review') AS review,
                      SUM(status='reject') AS rejected,
                      AVG(overall_confidence) AS avg_confidence
               FROM inspections"""
        ) or {}
        reasons: dict[str, int] = {}
        for row in self.db.fetch_all("SELECT reasons_json FROM inspections"):
            for reason in json.loads(row["reasons_json"]):
                if reason["severity"] != "info":
                    reasons[reason["code"]] = reasons.get(reason["code"], 0) + 1
        trend = self.db.fetch_all(
            """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS total,
                      SUM(status='pass') AS passed, SUM(status='review') AS review,
                      SUM(status='reject') AS rejected
               FROM inspections GROUP BY substr(created_at, 1, 10) ORDER BY day DESC LIMIT 14"""
        )
        latencies = sorted(
            int(row["processing_ms"] or 0)
            for row in self.db.fetch_all("SELECT processing_ms FROM inspections")
        )
        def percentile(fraction: float) -> int:
            if not latencies:
                return 0
            return latencies[min(round((len(latencies) - 1) * fraction), len(latencies) - 1)]

        by_workstation = self.db.fetch_all(
            """SELECT COALESCE(w.code, 'UNASSIGNED') AS workstation, COUNT(*) AS total,
                      SUM(i.status='pass') AS passed, SUM(i.status='reject') AS rejected,
                      AVG(i.processing_ms) AS avg_processing_ms
               FROM inspections i LEFT JOIN workstations w ON w.id=i.workstation_id
               GROUP BY COALESCE(w.code, 'UNASSIGNED') ORDER BY total DESC"""
        )
        total_count = int(totals.get("total") or 0)
        return {
            "total": total_count,
            "passed": totals.get("passed") or 0,
            "review": totals.get("review") or 0,
            "rejected": totals.get("rejected") or 0,
            "avg_confidence": round(totals.get("avg_confidence") or 0.0, 4),
            "top_reasons": sorted(
                ({"code": key, "count": value} for key, value in reasons.items()),
                key=lambda item: item["count"], reverse=True,
            )[:5],
            "daily_trend": list(reversed(trend)),
            "pass_rate": round((totals.get("passed") or 0) / total_count, 4) if total_count else 0.0,
            "review_rate": round((totals.get("review") or 0) / total_count, 4) if total_count else 0.0,
            "reject_rate": round((totals.get("rejected") or 0) / total_count, 4) if total_count else 0.0,
            "latency_ms": {"p50": percentile(0.5), "p95": percentile(0.95), "p99": percentile(0.99)},
            "by_workstation": by_workstation,
        }

    def list_inspections(
        self, *, status: str | None = None, order_id: int | None = None,
        serial: str | None = None, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("i.status = ?")
            params.append(status)
        if order_id:
            clauses.append("i.order_id = ?")
            params.append(order_id)
        if serial:
            clauses.append("i.serial_number LIKE ?")
            params.append(f"%{serial}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self.db.fetch_one(f"SELECT COUNT(*) AS count FROM inspections i {where}", tuple(params))
        rows = self.db.fetch_all(
            f"""SELECT i.id, i.serial_number, i.recognized_model, i.batch_number, i.status,
                       i.overall_confidence, i.processing_ms, i.created_at, o.order_number,
                       CASE WHEN i.image_sha256 != '' THEN 1 ELSE 0 END AS has_evidence,
                       w.code AS workstation_code
                FROM inspections i JOIN production_orders o ON o.id = i.order_id
                LEFT JOIN workstations w ON w.id = i.workstation_id
                {where} ORDER BY i.created_at DESC LIMIT ? OFFSET ?""",
            tuple(params + [min(max(limit, 1), 200), max(offset, 0)]),
        )
        return {"items": rows, "total": int((total or {}).get("count", 0)), "limit": limit, "offset": offset}

    def create_workstation(self, code: str, name: str, location: str) -> int:
        workstation_id = self.db.execute(
            "INSERT INTO workstations(code, name, location, active, created_at) VALUES (?, ?, ?, 1, ?)",
            (code, name, location, utc_now()),
        )
        self.db.audit("workstation", str(workstation_id), "created", {"code": code})
        return workstation_id

    def list_workstations(self) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """SELECT id, code, name, location, active, last_seen_at, software_version, created_at
               FROM workstations ORDER BY code"""
        )

    def get_workstation(self, workstation_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one(
            """SELECT id, code, name, location, active, last_seen_at, software_version, created_at
               FROM workstations WHERE id = ?""",
            (workstation_id,),
        )

    def heartbeat_workstation(self, workstation_id: int) -> dict[str, Any] | None:
        if not self.get_workstation(workstation_id):
            return None
        self.db.execute("UPDATE workstations SET last_seen_at = ? WHERE id = ?", (utc_now(), workstation_id))
        return self.get_workstation(workstation_id)

    def issue_workstation_key(self, workstation_id: int, actor: str) -> str | None:
        if not self.get_workstation(workstation_id):
            return None
        raw_key = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw_key.encode()).hexdigest()
        self.db.execute("UPDATE workstations SET api_key_hash = ? WHERE id = ?", (digest, workstation_id))
        self.db.audit("workstation", str(workstation_id), "api_key_rotated", {"actor": actor})
        return raw_key

    def device_heartbeat(self, code: str, raw_key: str, software_version: str) -> dict[str, Any] | None:
        digest = hashlib.sha256(raw_key.encode()).hexdigest()
        station = self.db.fetch_one(
            "SELECT * FROM workstations WHERE code = ? AND api_key_hash = ? AND active = 1",
            (code, digest),
        )
        if not station:
            return None
        self.db.execute(
            "UPDATE workstations SET last_seen_at = ?, software_version = ? WHERE id = ?",
            (utc_now(), software_version[:80], station["id"]),
        )
        return self.get_workstation(station["id"])

    def list_audit_logs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?""",
            (min(max(limit, 1), 500), max(offset, 0)),
        )
        return [decode_json_columns(row, ("details_json",)) for row in rows]  # type: ignore[list-item]

    def export_inspections(self) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """SELECT i.id, i.serial_number, i.recognized_model, i.batch_number,
                      i.status, i.overall_confidence, o.order_number, p.code AS product_code,
                      i.created_at
               FROM inspections i
               JOIN production_orders o ON o.id = i.order_id
               JOIN product_models p ON p.id = i.product_model_id
               ORDER BY i.created_at DESC"""
        )

    def list_outbox_events(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT * FROM outbox_events WHERE status = ? ORDER BY created_at LIMIT ?",
            (status, min(max(limit, 1), 500)),
        )
        return [decode_json_columns(row, ("payload_json",)) for row in rows]  # type: ignore[list-item]

    def acknowledge_outbox_event(self, event_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            cursor = conn.execute(
                """UPDATE outbox_events SET status='processed', processed_at=?, attempts=attempts+1
                   WHERE id=? AND status='pending'""",
                (utc_now(), event_id),
            )
            if cursor.rowcount != 1:
                return None
        row = self.db.fetch_one("SELECT * FROM outbox_events WHERE id = ?", (event_id,))
        return decode_json_columns(row, ("payload_json",))

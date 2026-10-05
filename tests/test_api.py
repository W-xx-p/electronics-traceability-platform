from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from backend.app.config import Settings
from backend.app.main import create_app
from scripts.backup_database import backup


def make_png(serial: str, *, confidence: float = 0.97, quality: float = 0.95,
             model: str = "ECU-A100", qr_serial: str | None = None) -> bytes:
    values = {
        "serial_number": serial,
        "product_model": model,
        "batch_number": "B260001",
        "manufacturing_date": "2026-10-05",
        "rohs": "ROHS",
    }
    data = {
        "fields": {key: {"value": value, "confidence": confidence} for key, value in values.items()},
        "qr_payload": {
            "serial_number": qr_serial or serial,
            "product_model": model,
            "batch_number": "B260001",
        },
        "quality": {"score": quality, "issues": []},
        "raw_text": "fixture",
    }
    image = Image.new("RGB", (300, 160), "white")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("electrotrace", json.dumps(data))
    target = io.BytesIO()
    image.save(target, format="PNG", pnginfo=metadata)
    return target.getvalue()


def client_for(tmp_path, *, auth_required: bool = False, max_login_attempts: int = 5):
    settings = Settings(
        base_dir=tmp_path,
        database_path=tmp_path / "test.db",
        upload_dir=tmp_path / "uploads",
        static_dir=tmp_path / "no-static",
        ocr_provider="demo",
        seed_demo=True,
        auth_required=auth_required,
        max_login_attempts=max_login_attempts,
    )
    return TestClient(create_app(settings))


def inspect(client: TestClient, image: bytes, key: str = "request-1"):
    return client.post(
        "/api/v1/inspections",
        data={"order_id": "1"},
        files={"image": ("label.png", image, "image/png")},
        headers={"Idempotency-Key": key},
    )


def test_pass_trace_duplicate_and_idempotency(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get("/health/ready").json()["status"] == "ready"
        image = make_png("A100-20260001")
        first = inspect(client, image)
        assert first.status_code == 201
        assert first.json()["status"] == "pass"

        same_request = inspect(client, image)
        assert same_request.status_code == 201
        assert same_request.json()["id"] == first.json()["id"]

        duplicate = inspect(client, image, key="request-2")
        assert duplicate.status_code == 201
        assert duplicate.json()["status"] == "reject"
        assert any(reason["code"] == "duplicate_serial" for reason in duplicate.json()["reasons"])

        trace = client.get("/api/v1/trace/A100-20260001")
        assert trace.status_code == 200
        assert len(trace.json()["inspections"]) == 2


def test_review_and_resolution(tmp_path):
    with client_for(tmp_path) as client:
        response = inspect(
            client,
            make_png("A100-20260002", confidence=0.65, quality=0.55),
        )
        assert response.json()["status"] == "review"
        reviews = client.get("/api/v1/reviews").json()
        assert len(reviews) == 1

        resolved = client.post(
            f"/api/v1/reviews/{reviews[0]['id']}/resolve",
            json={
                "resolution": "pass",
                "reviewer": "quality-engineer",
                "reason": "人工确认印刷内容正确",
                "corrected_fields": {},
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "pass"
        assert client.get("/api/v1/reviews").json() == []


def test_model_and_qr_conflicts_reject(tmp_path):
    with client_for(tmp_path) as client:
        wrong_model = inspect(
            client,
            make_png("A100-20260003", model="ECU-B200"),
            key="wrong-model",
        )
        assert wrong_model.json()["status"] == "reject"
        assert any(reason["code"] == "order_model_mismatch" for reason in wrong_model.json()["reasons"])

        qr_conflict = inspect(
            client,
            make_png("A100-20260004", qr_serial="A100-20269999"),
            key="qr-conflict",
        )
        assert qr_conflict.json()["status"] == "reject"
        assert any(reason["code"] == "ocr_qr_mismatch" for reason in qr_conflict.json()["reasons"])


def test_rejects_non_image_content_type(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/v1/inspections",
            data={"order_id": "1"},
            files={"image": ("payload.txt", b"not an image", "text/plain")},
        )
        assert response.status_code == 422
        assert "仅支持" in response.json()["detail"]


def test_rejects_spoofed_image_content(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/v1/inspections",
            data={"order_id": "1"},
            files={"image": ("fake.png", b"not really a png", "image/png")},
        )
        assert response.status_code == 422
        assert "有效" in response.json()["detail"]

        oversized = client.post(
            "/api/v1/inspections",
            data={"order_id": "1"},
            files={"image": ("large.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")},
        )
        assert oversized.status_code == 413


def test_authentication_rbac_audit_export_and_metrics(tmp_path):
    with client_for(tmp_path, auth_required=True) as client:
        assert client.get("/api/v1/orders").status_code == 401

        operator_login = client.post(
            "/api/v1/auth/login",
            json={"username": "operator", "password": "Operator!2026"},
        )
        assert operator_login.status_code == 200
        operator_headers = {"Authorization": f"Bearer {operator_login.json()['access_token']}"}
        assert client.get("/api/v1/orders", headers=operator_headers).status_code == 200
        assert client.get("/api/v1/reviews", headers=operator_headers).status_code == 403
        assert client.get("/api/v1/audit-logs", headers=operator_headers).status_code == 403

        quality_login = client.post(
            "/api/v1/auth/login",
            json={"username": "quality", "password": "Quality!2026"},
        ).json()
        quality_headers = {"Authorization": f"Bearer {quality_login['access_token']}"}
        assert client.get("/api/v1/reviews", headers=quality_headers).status_code == 200
        export = client.get("/api/v1/exports/inspections.csv", headers=quality_headers)
        assert export.status_code == 200
        assert export.content.startswith(b"\xef\xbb\xbf")

        admin_login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "ChangeMe!2026"},
        ).json()
        admin_headers = {"Authorization": f"Bearer {admin_login['access_token']}"}
        audit = client.get("/api/v1/audit-logs", headers=admin_headers)
        assert audit.status_code == 200
        assert any(item["action"] == "login" for item in audit.json())

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "electrotrace_http_requests_total" in metrics.text
        assert metrics.headers["x-request-id"]


def test_enterprise_management_rule_versions_and_workstation_trace(tmp_path):
    with client_for(tmp_path, auth_required=True) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "ChangeMe!2026"}
        ).json()
        headers = {"Authorization": f"Bearer {login['access_token']}"}

        stations = client.get("/api/v1/workstations", headers=headers).json()
        assert stations[0]["code"] == "LINE-A-01"
        heartbeat = client.post(
            f"/api/v1/workstations/{stations[0]['id']}/heartbeat", headers=headers
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["last_seen_at"]

        inspection = client.post(
            "/api/v1/inspections",
            data={"order_id": "1", "workstation_id": str(stations[0]["id"])},
            files={"image": ("label.png", make_png("A100-20260099"), "image/png")},
            headers={**headers, "Idempotency-Key": "station-test"},
        )
        assert inspection.status_code == 201
        assert inspection.json()["workstation_id"] == stations[0]["id"]
        assert inspection.json()["processing_ms"] >= 0

        records = client.get(
            "/api/v1/inspections?inspection_status=pass&serial=60099", headers=headers
        ).json()
        assert records["total"] == 1
        assert records["items"][0]["workstation_code"] == "LINE-A-01"
        assert client.get("/api/v1/dashboard/summary", headers=headers).json()["daily_trend"]

        model = client.get("/api/v1/product-models", headers=headers).json()[0]
        versions = client.get(
            f"/api/v1/product-models/{model['id']}/rule-versions", headers=headers
        ).json()
        assert versions[0]["version"] == 1
        updated = client.post(
            f"/api/v1/product-models/{model['id']}/rule-versions",
            headers=headers,
            json={"rules": model["rules"], "change_reason": "回归测试规则版本"},
        )
        assert updated.status_code == 200
        versions = client.get(
            f"/api/v1/product-models/{model['id']}/rule-versions", headers=headers
        ).json()
        assert [item["version"] for item in versions] == [2, 1]
        invalid_rules = {**model["rules"], "fields": {"serial_number": {"pattern": "["}}}
        invalid = client.post(
            f"/api/v1/product-models/{model['id']}/rule-versions",
            headers=headers,
            json={"rules": invalid_rules, "change_reason": "无效规则必须被拒绝"},
        )
        assert invalid.status_code == 422

        created = client.post(
            "/api/v1/users", headers=headers,
            json={
                "username": "line2", "password": "LineTwo!2026#",
                "display_name": "二号线操作员", "role": "operator",
            },
        )
        assert created.status_code == 201
        disabled = client.patch(
            f"/api/v1/users/{created.json()['id']}/status",
            headers=headers, json={"active": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["active"] == 0
        audit = client.get("/api/v1/audit-logs?limit=20", headers=headers).json()
        user_event = next(item for item in audit if item["entity_type"] == "user" and item["entity_id"] == str(created.json()["id"]))
        assert user_event["details"]["request_id"]
        assert user_event["details"]["actor"] == "admin"

        target_dir = tmp_path / "backups"
        backed_up = backup(tmp_path / "test.db", target_dir, keep=2)
        assert backed_up.exists()
        assert backed_up.with_suffix(".db.sha256").exists()
        with __import__("sqlite3").connect(backed_up) as restored:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_production_configuration_rejects_demo_seed(tmp_path):
    settings = Settings(
        base_dir=tmp_path,
        database_path=tmp_path / "prod.db",
        upload_dir=tmp_path / "uploads",
        static_dir=tmp_path / "no-static",
        environment="production",
        seed_demo=True,
    )
    try:
        create_app(settings)
        assert False, "production mode should reject demo seed"
    except RuntimeError as exc:
        assert "SEED_DEMO=false" in str(exc)


def test_production_hides_docs_and_protects_metrics(tmp_path):
    token = "metrics-token-with-at-least-24-characters"
    settings = Settings(
        base_dir=tmp_path,
        database_path=tmp_path / "prod.db",
        upload_dir=tmp_path / "uploads",
        static_dir=tmp_path / "no-static",
        environment="production",
        seed_demo=False,
        audit_hmac_key="a" * 32,
        metrics_token=token,
        bootstrap_admin_username="bootstrap-admin",
        bootstrap_admin_password="Bootstrap!2026#",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"X-Metrics-Token": token}).status_code == 200


def test_evidence_order_state_device_key_and_audit_integrity(tmp_path):
    with client_for(tmp_path, auth_required=True) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "ChangeMe!2026"}
        ).json()
        headers = {"Authorization": f"Bearer {login['access_token']}"}
        station = client.get("/api/v1/workstations", headers=headers).json()[0]

        key_result = client.post(
            f"/api/v1/workstations/{station['id']}/rotate-key", headers=headers
        )
        assert key_result.status_code == 200
        device = client.post(
            "/api/v1/device/heartbeat",
            headers={
                "X-Workstation-Code": station["code"],
                "X-Workstation-Key": key_result.json()["api_key"],
                "X-Software-Version": "station-agent/1.0",
            },
        )
        assert device.status_code == 200

        result = client.post(
            "/api/v1/inspections",
            data={"order_id": "1", "workstation_id": str(station["id"])},
            files={"image": ("label.png", make_png("A100-20260111"), "image/png")},
            headers={**headers, "Idempotency-Key": "evidence-test-01"},
        ).json()
        assert "image_path" not in result
        assert len(result["image_sha256"]) == 64
        evidence = client.get(f"/api/v1/inspections/{result['id']}/image", headers=headers)
        assert evidence.status_code == 200
        assert evidence.content.startswith(b"\x89PNG")
        outbox = client.get("/api/v1/integration/outbox", headers=headers).json()
        event = next(item for item in outbox if item["aggregate_id"] == result["id"])
        assert event["event_type"] == "inspection.completed"
        assert client.post(
            f"/api/v1/integration/outbox/{event['id']}/ack", headers=headers
        ).status_code == 200
        assert client.post(
            f"/api/v1/integration/outbox/{event['id']}/ack", headers=headers
        ).status_code == 404

        completed = client.patch(
            "/api/v1/orders/1/status", headers=headers, json={"status": "completed"}
        )
        assert completed.status_code == 200
        blocked = client.post(
            "/api/v1/inspections",
            data={"order_id": "1"},
            files={"image": ("label.png", make_png("A100-20260112"), "image/png")},
            headers={**headers, "Idempotency-Key": "closed-order-01"},
        )
        assert blocked.status_code == 422
        assert client.patch(
            "/api/v1/orders/1/status", headers=headers, json={"status": "active"}
        ).status_code == 200

        integrity = client.get("/api/v1/audit-logs/verify", headers=headers).json()
        assert integrity["valid"] is True
        client.app.state.database.execute(
            "UPDATE audit_logs SET details_json = ? WHERE id = 1", ('{"tampered":true}',)
        )
        assert client.get("/api/v1/audit-logs/verify", headers=headers).json()["valid"] is False


def test_login_lockout_and_review_claim_conflict(tmp_path):
    with client_for(tmp_path, auth_required=True, max_login_attempts=2) as client:
        for _ in range(2):
            assert client.post(
                "/api/v1/auth/login", json={"username": "operator", "password": "WrongPassword!"}
            ).status_code == 401
        assert client.post(
            "/api/v1/auth/login", json={"username": "operator", "password": "Operator!2026"}
        ).status_code == 401

        quality = client.post(
            "/api/v1/auth/login", json={"username": "quality", "password": "Quality!2026"}
        ).json()
        admin = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "ChangeMe!2026"}
        ).json()
        quality_headers = {"Authorization": f"Bearer {quality['access_token']}"}
        admin_headers = {"Authorization": f"Bearer {admin['access_token']}"}
        inspection = client.post(
            "/api/v1/inspections",
            data={"order_id": "1"},
            files={"image": ("label.png", make_png("A100-20260121", confidence=0.6), "image/png")},
            headers={**quality_headers, "Idempotency-Key": "claim-review-01"},
        )
        assert inspection.json()["status"] == "review"
        review_id = client.get("/api/v1/reviews", headers=quality_headers).json()[0]["id"]
        assert client.post(f"/api/v1/reviews/{review_id}/claim", headers=quality_headers).status_code == 200
        assert client.post(f"/api/v1/reviews/{review_id}/claim", headers=admin_headers).status_code == 409
        resolved = client.post(
            f"/api/v1/reviews/{review_id}/resolve", headers=quality_headers,
            json={
                "resolution": "pass", "reviewer": "ignored", "reason": "认领后确认",
                "corrected_fields": {},
            },
        )
        assert resolved.status_code == 200

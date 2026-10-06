from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    database_path: Path
    upload_dir: Path
    static_dir: Path
    ocr_provider: str = "demo"
    seed_demo: bool = True
    demo_inspection_count: int = 0
    auth_required: bool = True
    demo_admin_password: str = "ChangeMe!2026"
    session_hours: int = 8
    max_requests_per_minute: int = 120
    environment: str = "development"
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    max_login_attempts: int = 5
    login_lock_minutes: int = 15
    audit_hmac_key: str = ""
    metrics_token: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        backend_dir = Path(__file__).resolve().parents[1]
        project_dir = backend_dir.parent
        data_dir = project_dir / "data"
        return cls(
            base_dir=project_dir,
            database_path=Path(os.getenv("ELECTROTRACE_DB", data_dir / "electrotrace.db")),
            upload_dir=Path(os.getenv("ELECTROTRACE_UPLOAD_DIR", data_dir / "uploads")),
            static_dir=Path(os.getenv("ELECTROTRACE_STATIC_DIR", project_dir / "frontend")),
            ocr_provider=os.getenv("ELECTROTRACE_OCR_PROVIDER", "demo").lower(),
            seed_demo=os.getenv("ELECTROTRACE_SEED_DEMO", "true").lower() == "true",
            demo_inspection_count=max(0, int(os.getenv("ELECTROTRACE_DEMO_INSPECTION_COUNT", "0"))),
            auth_required=os.getenv("ELECTROTRACE_AUTH_REQUIRED", "true").lower() == "true",
            demo_admin_password=os.getenv("ELECTROTRACE_DEMO_ADMIN_PASSWORD", "ChangeMe!2026"),
            session_hours=int(os.getenv("ELECTROTRACE_SESSION_HOURS", "8")),
            max_requests_per_minute=int(os.getenv("ELECTROTRACE_RATE_LIMIT", "120")),
            environment=os.getenv("ELECTROTRACE_ENV", "development").lower(),
            allowed_hosts=tuple(
                item.strip() for item in os.getenv(
                    "ELECTROTRACE_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
                ).split(",") if item.strip()
            ),
            bootstrap_admin_username=os.getenv("ELECTROTRACE_BOOTSTRAP_ADMIN_USERNAME", ""),
            bootstrap_admin_password=os.getenv("ELECTROTRACE_BOOTSTRAP_ADMIN_PASSWORD", ""),
            max_login_attempts=int(os.getenv("ELECTROTRACE_MAX_LOGIN_ATTEMPTS", "5")),
            login_lock_minutes=int(os.getenv("ELECTROTRACE_LOGIN_LOCK_MINUTES", "15")),
            audit_hmac_key=os.getenv("ELECTROTRACE_AUDIT_HMAC_KEY", ""),
            metrics_token=os.getenv("ELECTROTRACE_METRICS_TOKEN", ""),
        )

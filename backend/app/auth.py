from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database, utc_now


PBKDF2_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        iterations_text, salt_hex, digest_hex = encoded.split("$", 2)
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations_text)
        )
        return hmac.compare_digest(calculated.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(
        self, db: Database, session_hours: int = 8,
        max_login_attempts: int = 5, login_lock_minutes: int = 15,
    ):
        self.db = db
        self.session_hours = session_hours
        self.max_login_attempts = max(max_login_attempts, 1)
        self.login_lock_minutes = max(login_lock_minutes, 1)

    def seed_demo_users(self, admin_password: str) -> None:
        demo_users = (
            ("admin", admin_password, "系统管理员", "admin"),
            ("quality", "Quality!2026", "质量工程师", "quality_engineer"),
            ("operator", "Operator!2026", "产线操作员", "operator"),
            ("viewer", "Viewer!2026", "只读访客", "viewer"),
        )
        for username, password, display_name, role in demo_users:
            if self.db.fetch_one("SELECT id FROM users WHERE username = ?", (username,)):
                continue
            self.db.execute(
                """INSERT INTO users(username, password_hash, display_name, role, active, created_at)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                (username, hash_password(password), display_name, role, utc_now()),
            )

    def has_users(self) -> bool:
        return self.db.fetch_one("SELECT id FROM users LIMIT 1") is not None

    def login(self, username: str, password: str) -> tuple[str, dict[str, Any]] | None:
        user = self.db.fetch_one(
            """SELECT id, username, password_hash, display_name, role, active,
                      failed_attempts, locked_until
               FROM users WHERE username = ?""",
            (username,),
        )
        now = datetime.now(UTC)
        if not user or not user["active"]:
            return None
        if user["locked_until"]:
            try:
                if datetime.fromisoformat(user["locked_until"]) > now:
                    return None
            except ValueError:
                return None
        if not verify_password(password, user["password_hash"]):
            attempts = int(user["failed_attempts"] or 0) + 1
            locked_until = None
            if attempts >= self.max_login_attempts:
                locked_until = (now + timedelta(minutes=self.login_lock_minutes)).isoformat()
                attempts = 0
            self.db.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                (attempts, locked_until, user["id"]),
            )
            self.db.audit("user", str(user["id"]), "login_failed", {"username": username, "locked": bool(locked_until)})
            return None
        token = secrets.token_urlsafe(32)
        expires = now + timedelta(hours=self.session_hours)
        self.db.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
            (now.isoformat(), user["id"]),
        )
        self.db.execute(
            """INSERT INTO auth_sessions(user_id, token_hash, expires_at, revoked, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (user["id"], token_digest(token), expires.isoformat(), utc_now()),
        )
        safe_user = {key: user[key] for key in ("id", "username", "display_name", "role")}
        self.db.audit("user", str(user["id"]), "login", {"username": username})
        return token, safe_user

    def authenticate(self, token: str) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        return self.db.fetch_one(
            """SELECT u.id, u.username, u.display_name, u.role
               FROM auth_sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.revoked = 0 AND s.expires_at > ? AND u.active = 1""",
            (token_digest(token), now),
        )

    def logout(self, token: str) -> None:
        self.db.execute(
            "UPDATE auth_sessions SET revoked = 1 WHERE token_hash = ?",
            (token_digest(token),),
        )

    def cleanup_expired(self) -> None:
        self.db.execute(
            "DELETE FROM auth_sessions WHERE expires_at <= ? OR revoked = 1",
            (datetime.now(UTC).isoformat(),),
        )

    def list_users(self) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            "SELECT id, username, display_name, role, active, created_at FROM users ORDER BY id"
        )

    def create_user(self, username: str, password: str, display_name: str, role: str) -> int:
        return self.db.execute(
            """INSERT INTO users(username, password_hash, display_name, role, active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (username, hash_password(password), display_name, role, utc_now()),
        )

    def set_user_active(self, user_id: int, active: bool) -> bool:
        user = self.db.fetch_one("SELECT id, role, active FROM users WHERE id = ?", (user_id,))
        if not user:
            return False
        if not active and user["role"] == "admin" and user["active"]:
            admins = self.db.fetch_one(
                "SELECT COUNT(*) AS count FROM users WHERE role = 'admin' AND active = 1"
            )
            if admins and admins["count"] <= 1:
                raise ValueError("不能停用最后一个管理员账号")
        self.db.execute(
            "UPDATE users SET active = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (int(active), user_id),
        )
        if not active:
            self.db.execute("UPDATE auth_sessions SET revoked = 1 WHERE user_id = ?", (user_id,))
        return True

    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        user = self.db.fetch_one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        if not user or not verify_password(current_password, user["password_hash"]):
            return False
        self.db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        self.db.execute("UPDATE auth_sessions SET revoked = 1 WHERE user_id = ?", (user_id,))
        return True

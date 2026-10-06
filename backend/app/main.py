from __future__ import annotations

import csv
import hmac
import io
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import AuthService
from .config import Settings
from .context import request_context
from .db import Database
from .middleware import EnterpriseMiddleware, Metrics
from .ocr import build_ocr_provider
from .repository import Repository
from .rules import validate_rule_config
from .schemas import (
    LoginRequest, OrderStatusUpdate, PasswordChange, ProductModelCreate, ProductionOrderCreate,
    ReviewResolve, RuleVersionCreate, UserCreate, UserStatusUpdate, WorkstationCreate,
)
from .service import InspectionService, MAX_FILE_SIZE


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.from_env()
    if app_settings.environment == "production" and app_settings.seed_demo:
        raise RuntimeError("生产环境必须设置 ELECTROTRACE_SEED_DEMO=false")
    if app_settings.environment == "production" and app_settings.demo_inspection_count:
        raise RuntimeError("生产环境必须设置 ELECTROTRACE_DEMO_INSPECTION_COUNT=0")
    if app_settings.environment == "production" and len(app_settings.audit_hmac_key) < 32:
        raise RuntimeError("生产环境必须设置至少 32 位的 ELECTROTRACE_AUDIT_HMAC_KEY")
    if app_settings.environment == "production" and len(app_settings.metrics_token) < 24:
        raise RuntimeError("生产环境必须设置至少 24 位的 ELECTROTRACE_METRICS_TOKEN")
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(app_settings.database_path, app_settings.audit_hmac_key)
        db.initialize()
        repository = Repository(db)
        auth_service = AuthService(
            db, app_settings.session_hours,
            app_settings.max_login_attempts, app_settings.login_lock_minutes,
        )
        if app_settings.seed_demo:
            repository.seed_demo()
            if app_settings.demo_inspection_count:
                repository.seed_demo_inspections(app_settings.demo_inspection_count)
            auth_service.seed_demo_users(app_settings.demo_admin_password)
        elif not auth_service.has_users():
            if not app_settings.bootstrap_admin_username or len(app_settings.bootstrap_admin_password) < 12:
                raise RuntimeError(
                    "首次生产启动必须设置 ELECTROTRACE_BOOTSTRAP_ADMIN_USERNAME "
                    "和至少 12 位的 ELECTROTRACE_BOOTSTRAP_ADMIN_PASSWORD"
                )
            auth_service.create_user(
                app_settings.bootstrap_admin_username,
                app_settings.bootstrap_admin_password,
                "初始系统管理员",
                "admin",
            )
        auth_service.cleanup_expired()
        app.state.settings = app_settings
        app.state.database = db
        app.state.repository = repository
        app.state.auth_service = auth_service
        app.state.inspection_service = InspectionService(
            repository, build_ocr_provider(app_settings.ocr_provider), app_settings.upload_dir
        )
        yield

    app = FastAPI(
        title="ElectroTrace API", version="1.0.0",
        description="电子产品序列号识别、质量判定与追溯平台", lifespan=lifespan,
        docs_url=None if app_settings.environment == "production" else "/docs",
        redoc_url=None if app_settings.environment == "production" else "/redoc",
        openapi_url=None if app_settings.environment == "production" else "/openapi.json",
    )
    app.add_middleware(
        EnterpriseMiddleware,
        limit_per_minute=app_settings.max_requests_per_minute,
        metrics=metrics,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(app_settings.allowed_hosts))
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = f"HTTP_{exc.status_code}"
        return JSONResponse(
            {"detail": exc.detail, "error_code": code, "request_id": getattr(request.state, "request_id", None)},
            status_code=exc.status_code, headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "detail": "请求参数校验失败", "error_code": "VALIDATION_ERROR",
                "request_id": getattr(request.state, "request_id", None), "errors": exc.errors(),
            },
            status_code=422,
        )

    def repo() -> Repository:
        return app.state.repository

    def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
        if not app_settings.auth_required:
            return {"id": 0, "username": "system", "display_name": "测试系统", "role": "admin"}
        if not credentials or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="请先登录", headers={"WWW-Authenticate": "Bearer"})
        user = app.state.auth_service.authenticate(credentials.credentials)
        if not user:
            raise HTTPException(status_code=401, detail="登录已过期，请重新登录", headers={"WWW-Authenticate": "Bearer"})
        request_context.get()["actor"] = user["username"]
        return user

    def require_roles(*roles: str) -> Callable:
        def dependency(user: dict = Depends(current_user)) -> dict:
            if user["role"] not in roles:
                raise HTTPException(status_code=403, detail="当前角色无权执行此操作")
            return user
        return dependency

    @app.get("/health/live", tags=["health"])
    def liveness() -> dict:
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"])
    def readiness() -> dict:
        try:
            app.state.database.fetch_one("SELECT 1 AS ok")
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ready", "ocr_provider": app_settings.ocr_provider}

    @app.get("/metrics", response_class=PlainTextResponse, tags=["operations"])
    def prometheus_metrics(
        metrics_token: str = Header(default="", alias="X-Metrics-Token"),
    ) -> str:
        if app_settings.environment == "production" and not hmac.compare_digest(
            metrics_token, app_settings.metrics_token
        ):
            raise HTTPException(status_code=401, detail="监控凭据无效")
        return metrics.render()

    @app.post("/api/v1/auth/login", tags=["authentication"])
    def login(payload: LoginRequest) -> dict:
        result = app.state.auth_service.login(payload.username, payload.password)
        if not result:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token, user = result
        return {"access_token": token, "token_type": "bearer", "expires_in_hours": app_settings.session_hours, "user": user}

    @app.get("/api/v1/auth/me", tags=["authentication"])
    def me(user: dict = Depends(current_user)) -> dict:
        return user

    @app.post("/api/v1/auth/logout", status_code=204, tags=["authentication"])
    def logout(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        _user: dict = Depends(current_user),
    ) -> None:
        if credentials:
            app.state.auth_service.logout(credentials.credentials)

    @app.post("/api/v1/auth/change-password", status_code=204, tags=["authentication"])
    def change_password(payload: PasswordChange, user: dict = Depends(current_user)) -> None:
        if not app.state.auth_service.change_password(user["id"], payload.current_password, payload.new_password):
            raise HTTPException(status_code=400, detail="当前密码错误")
        app.state.database.audit("user", str(user["id"]), "password_changed", {"actor": user["username"]})

    @app.get("/api/v1/users", tags=["administration"])
    def list_users(_user: dict = Depends(require_roles("admin"))) -> list[dict]:
        return app.state.auth_service.list_users()

    @app.post("/api/v1/users", status_code=201, tags=["administration"])
    def create_user(payload: UserCreate, user: dict = Depends(require_roles("admin"))) -> dict:
        try:
            user_id = app.state.auth_service.create_user(
                payload.username, payload.password, payload.display_name, payload.role
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="用户名已存在") from exc
        app.state.database.audit("user", str(user_id), "created", {"actor": user["username"], "role": payload.role})
        return next(item for item in app.state.auth_service.list_users() if item["id"] == user_id)

    @app.patch("/api/v1/users/{user_id}/status", tags=["administration"])
    def update_user_status(user_id: int, payload: UserStatusUpdate, user: dict = Depends(require_roles("admin"))) -> dict:
        if user_id == user["id"] and not payload.active:
            raise HTTPException(status_code=400, detail="不能停用当前登录账号")
        try:
            found = app.state.auth_service.set_user_active(user_id, payload.active)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not found:
            raise HTTPException(status_code=404, detail="用户不存在")
        app.state.database.audit("user", str(user_id), "status_changed", {"actor": user["username"], "active": payload.active})
        return next(item for item in app.state.auth_service.list_users() if item["id"] == user_id)

    @app.get("/api/v1/product-models", tags=["configuration"])
    def list_product_models(_user: dict = Depends(current_user)) -> list[dict]:
        return repo().list_product_models()

    @app.post("/api/v1/product-models", status_code=201, tags=["configuration"])
    def create_product_model(payload: ProductModelCreate, _user: dict = Depends(require_roles("admin", "quality_engineer"))) -> dict:
        try:
            validate_rule_config(payload.rules)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            model_id = repo().create_product_model(payload.code, payload.name, payload.rules)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="产品型号编码已存在") from exc
        return repo().get_product_model(model_id) or {"id": model_id}

    @app.get("/api/v1/product-models/{model_id}/rule-versions", tags=["configuration"])
    def list_rule_versions(model_id: int, _user: dict = Depends(current_user)) -> list[dict]:
        if not repo().get_product_model(model_id):
            raise HTTPException(status_code=404, detail="产品型号不存在")
        return repo().list_rule_versions(model_id)

    @app.post("/api/v1/product-models/{model_id}/rule-versions", tags=["configuration"])
    def update_rules(
        model_id: int, payload: RuleVersionCreate,
        user: dict = Depends(require_roles("admin", "quality_engineer")),
    ) -> dict:
        try:
            validate_rule_config(payload.rules)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result = repo().update_product_rules(model_id, payload.rules, payload.change_reason, user["username"])
        if not result:
            raise HTTPException(status_code=404, detail="产品型号不存在")
        return result

    @app.get("/api/v1/orders", tags=["orders"])
    def list_orders(_user: dict = Depends(current_user)) -> list[dict]:
        return repo().list_orders()

    @app.post("/api/v1/orders", status_code=201, tags=["orders"])
    def create_order(payload: ProductionOrderCreate, _user: dict = Depends(require_roles("admin", "quality_engineer"))) -> dict:
        if not repo().get_product_model(payload.product_model_id):
            raise HTTPException(status_code=404, detail="产品型号不存在")
        try:
            order_id = repo().create_order(payload.order_number, payload.product_model_id, payload.batch_number, payload.target_quantity)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="生产订单号已存在") from exc
        return repo().get_order(order_id) or {"id": order_id}

    @app.patch("/api/v1/orders/{order_id}/status", tags=["orders"])
    def update_order_status(
        order_id: int, payload: OrderStatusUpdate,
        user: dict = Depends(require_roles("admin", "quality_engineer")),
    ) -> dict:
        try:
            result = repo().update_order_status(order_id, payload.status, user["username"])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="生产订单不存在")
        return result

    @app.get("/api/v1/workstations", tags=["workstations"])
    def list_workstations(_user: dict = Depends(current_user)) -> list[dict]:
        return repo().list_workstations()

    @app.post("/api/v1/workstations", status_code=201, tags=["workstations"])
    def create_workstation(payload: WorkstationCreate, user: dict = Depends(require_roles("admin"))) -> dict:
        try:
            workstation_id = repo().create_workstation(payload.code, payload.name, payload.location)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="工位编码已存在") from exc
        app.state.database.audit("workstation", str(workstation_id), "configured", {"actor": user["username"]})
        return repo().get_workstation(workstation_id) or {"id": workstation_id}

    @app.post("/api/v1/workstations/{workstation_id}/heartbeat", tags=["workstations"])
    def workstation_heartbeat(
        workstation_id: int,
        _user: dict = Depends(require_roles("admin", "quality_engineer", "operator")),
    ) -> dict:
        result = repo().heartbeat_workstation(workstation_id)
        if not result:
            raise HTTPException(status_code=404, detail="工位不存在")
        return result

    @app.post("/api/v1/workstations/{workstation_id}/rotate-key", tags=["workstations"])
    def rotate_workstation_key(
        workstation_id: int, user: dict = Depends(require_roles("admin")),
    ) -> dict:
        raw_key = repo().issue_workstation_key(workstation_id, user["username"])
        if not raw_key:
            raise HTTPException(status_code=404, detail="工位不存在")
        return {"workstation_id": workstation_id, "api_key": raw_key, "warning": "密钥仅显示一次，请立即安全保存"}

    @app.post("/api/v1/device/heartbeat", tags=["workstations"])
    def device_heartbeat(
        workstation_code: str = Header(alias="X-Workstation-Code"),
        workstation_key: str = Header(alias="X-Workstation-Key"),
        software_version: str = Header(default="unknown", alias="X-Software-Version"),
    ) -> dict:
        result = repo().device_heartbeat(workstation_code, workstation_key, software_version)
        if not result:
            raise HTTPException(status_code=401, detail="工位凭据无效")
        return {"status": "online", "workstation": result["code"], "server_time": result["last_seen_at"]}

    @app.post("/api/v1/inspections", status_code=201, tags=["inspection"])
    async def create_inspection(
        order_id: int = Form(...), image: UploadFile = File(...),
        workstation_id: int | None = Form(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        _user: dict = Depends(require_roles("admin", "quality_engineer", "operator")),
    ) -> dict:
        if idempotency_key and not 8 <= len(idempotency_key) <= 128:
            raise HTTPException(status_code=422, detail="Idempotency-Key 长度必须为 8 到 128 个字符")
        chunks: list[bytes] = []
        total = 0
        while chunk := await image.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail="图片不能超过 10 MiB")
            chunks.append(chunk)
        image_bytes = b"".join(chunks)
        try:
            return app.state.inspection_service.inspect(
                order_id=order_id, image_bytes=image_bytes, filename=image.filename or "upload.img",
                content_type=image.content_type or "application/octet-stream", idempotency_key=idempotency_key,
                workstation_id=workstation_id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="质检请求与已有记录冲突") from exc

    @app.get("/api/v1/inspections/{inspection_id}", tags=["inspection"])
    def get_inspection(inspection_id: str, _user: dict = Depends(current_user)) -> dict:
        item = repo().get_inspection(inspection_id)
        if not item:
            raise HTTPException(status_code=404, detail="质检记录不存在")
        return item

    @app.get("/api/v1/inspections/{inspection_id}/image", tags=["inspection"])
    def get_inspection_image(inspection_id: str, user: dict = Depends(current_user)) -> FileResponse:
        evidence = repo().get_evidence(inspection_id)
        if not evidence:
            raise HTTPException(status_code=404, detail="质检证据不存在")
        try:
            path = app.state.inspection_service.storage.resolve(evidence["image_path"])
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=410, detail="质检证据文件已不可用") from exc
        app.state.database.audit(
            "inspection", inspection_id, "evidence_viewed",
            {"actor": user["username"], "sha256": evidence["image_sha256"]},
        )
        return FileResponse(path, filename=f"inspection-{inspection_id}{path.suffix}", content_disposition_type="inline")

    @app.get("/api/v1/inspections", tags=["inspection"])
    def list_inspections(
        inspection_status: str | None = None, order_id: int | None = None,
        serial: str | None = None, limit: int = 50, offset: int = 0,
        _user: dict = Depends(current_user),
    ) -> dict:
        if inspection_status and inspection_status not in {"pass", "review", "reject", "void"}:
            raise HTTPException(status_code=422, detail="无效的质检状态")
        return repo().list_inspections(
            status=inspection_status, order_id=order_id, serial=serial, limit=limit, offset=offset
        )

    @app.get("/api/v1/trace/{serial_number}", tags=["traceability"])
    def trace_serial(serial_number: str, _user: dict = Depends(current_user)) -> dict:
        inspections = repo().trace_serial(serial_number)
        if not inspections:
            raise HTTPException(status_code=404, detail="未找到该序列号的追溯记录")
        return {"serial_number": serial_number, "inspections": inspections}

    @app.get("/api/v1/reviews", tags=["review"])
    def list_reviews(review_status: str = "open", _user: dict = Depends(require_roles("admin", "quality_engineer"))) -> list[dict]:
        return repo().list_reviews(review_status)

    @app.post("/api/v1/reviews/{review_id}/resolve", tags=["review"])
    def resolve_review(review_id: int, payload: ReviewResolve, user: dict = Depends(require_roles("admin", "quality_engineer"))) -> dict:
        try:
            inspection = repo().resolve_review(
                review_id, payload.resolution, user["username"], payload.reason, payload.corrected_fields
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="该序列号已有放行记录，不能重复放行") from exc
        if not inspection:
            raise HTTPException(status_code=404, detail="复核任务不存在或已处理")
        return inspection

    @app.post("/api/v1/reviews/{review_id}/claim", tags=["review"])
    def claim_review(
        review_id: int,
        user: dict = Depends(require_roles("admin", "quality_engineer")),
    ) -> dict:
        result = repo().claim_review(review_id, user["username"])
        if not result:
            raise HTTPException(status_code=409, detail="复核任务已被其他人员认领或已完成")
        return result

    @app.get("/api/v1/dashboard/summary", tags=["dashboard"])
    def dashboard_summary(_user: dict = Depends(current_user)) -> dict:
        return repo().dashboard()

    @app.get("/api/v1/audit-logs", tags=["operations"])
    def audit_logs(limit: int = 100, offset: int = 0, _user: dict = Depends(require_roles("admin"))) -> list[dict]:
        return repo().list_audit_logs(limit, offset)

    @app.get("/api/v1/audit-logs/verify", tags=["operations"])
    def verify_audit_logs(_user: dict = Depends(require_roles("admin"))) -> dict:
        return app.state.database.verify_audit_chain()

    @app.get("/api/v1/exports/inspections.csv", tags=["operations"])
    def export_inspections(_user: dict = Depends(require_roles("admin", "quality_engineer"))) -> StreamingResponse:
        output = io.StringIO()
        columns = ["id", "serial_number", "recognized_model", "batch_number", "status", "overall_confidence", "order_number", "product_code", "created_at"]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(repo().export_inspections())
        data = output.getvalue().encode("utf-8-sig")
        return StreamingResponse(
            iter([data]), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="electrotrace-inspections.csv"'},
        )

    @app.get("/api/v1/integration/outbox", tags=["integration"])
    def list_outbox(
        event_status: str = "pending", limit: int = 100,
        _user: dict = Depends(require_roles("admin")),
    ) -> list[dict]:
        if event_status not in {"pending", "processed"}:
            raise HTTPException(status_code=422, detail="无效的事件状态")
        return repo().list_outbox_events(event_status, limit)

    @app.post("/api/v1/integration/outbox/{event_id}/ack", tags=["integration"])
    def acknowledge_outbox(event_id: str, user: dict = Depends(require_roles("admin"))) -> dict:
        event = repo().acknowledge_outbox_event(event_id)
        if not event:
            raise HTTPException(status_code=404, detail="待处理事件不存在或已确认")
        app.state.database.audit("outbox_event", event_id, "acknowledged", {"actor": user["username"]})
        return event

    static_dir = Path(app_settings.static_dir)
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.get("/", include_in_schema=False)
        def frontend() -> FileResponse:
            return FileResponse(static_dir / "index.html")

    return app


app = create_app()

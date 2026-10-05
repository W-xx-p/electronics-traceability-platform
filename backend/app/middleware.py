from __future__ import annotations

import threading
import time
import uuid
import json
import logging
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .context import request_context


class Metrics:
    """Small dependency-free Prometheus collector for a single application process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.errors = 0
        self.inflight = 0
        self.latency_seconds = 0.0
        self.statuses: dict[int, int] = defaultdict(int)

    def start(self) -> None:
        with self._lock:
            self.inflight += 1

    def finish(self, status: int, elapsed: float) -> None:
        with self._lock:
            self.inflight -= 1
            self.requests += 1
            self.statuses[status] += 1
            self.latency_seconds += elapsed
            if status >= 500:
                self.errors += 1

    def render(self) -> str:
        with self._lock:
            lines = [
                "# HELP electrotrace_http_requests_total Total HTTP requests.",
                "# TYPE electrotrace_http_requests_total counter",
                f"electrotrace_http_requests_total {self.requests}",
                "# HELP electrotrace_http_errors_total Total 5xx responses.",
                "# TYPE electrotrace_http_errors_total counter",
                f"electrotrace_http_errors_total {self.errors}",
                "# HELP electrotrace_http_requests_inflight Current in-flight requests.",
                "# TYPE electrotrace_http_requests_inflight gauge",
                f"electrotrace_http_requests_inflight {self.inflight}",
                "# HELP electrotrace_http_request_duration_seconds_total Cumulative latency.",
                "# TYPE electrotrace_http_request_duration_seconds_total counter",
                f"electrotrace_http_request_duration_seconds_total {self.latency_seconds:.6f}",
            ]
            for status, count in sorted(self.statuses.items()):
                lines.append(f'electrotrace_http_responses_total{{status="{status}"}} {count}')
            return "\n".join(lines) + "\n"


class EnterpriseMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limit_per_minute: int, metrics: Metrics):
        super().__init__(app)
        self.limit = max(limit_per_minute, 1)
        self.metrics = metrics
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.logger = logging.getLogger("electrotrace.access")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        context = {
            "request_id": request_id,
            "client_ip": request.client.host if request.client else None,
        }
        context_token = request_context.set(context)
        started = time.perf_counter()
        self.metrics.start()
        status = 500
        try:
            if request.url.path not in {"/health/live", "/health/ready", "/metrics"}:
                client = request.client.host if request.client else "unknown"
                now = time.monotonic()
                with self._lock:
                    bucket = self._requests[client]
                    while bucket and bucket[0] < now - 60:
                        bucket.popleft()
                    if len(bucket) >= self.limit:
                        status = 429
                        return JSONResponse(
                            {"detail": "请求过于频繁，请稍后重试", "request_id": request_id},
                            status_code=status,
                            headers={"Retry-After": "60", "X-Request-ID": request_id},
                        )
                    bucket.append(now)
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'"
            )
            return response
        finally:
            elapsed = time.perf_counter() - started
            self.metrics.finish(status, elapsed)
            self.logger.info(json.dumps({
                "event": "http_request", "request_id": request_id,
                "method": request.method, "path": request.url.path,
                "status": status, "duration_ms": round(elapsed * 1000, 2),
                "client_ip": request.client.host if request.client else None,
                "actor": context.get("actor"),
            }, ensure_ascii=False))
            request_context.reset(context_token)

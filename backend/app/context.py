from __future__ import annotations

from contextvars import ContextVar
from typing import Any


request_context: ContextVar[dict[str, Any]] = ContextVar("request_context", default={})

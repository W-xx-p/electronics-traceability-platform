from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def strong_password(value: str) -> str:
    categories = (
        any(char.islower() for char in value),
        any(char.isupper() for char in value),
        any(char.isdigit() for char in value),
        any(not char.isalnum() for char in value),
    )
    if not all(categories):
        raise ValueError("密码必须同时包含大写字母、小写字母、数字和特殊字符")
    return value


class ProductModelCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    rules: dict[str, Any]


class ProductionOrderCreate(BaseModel):
    order_number: str = Field(min_length=2, max_length=64)
    product_model_id: int
    batch_number: str = Field(min_length=1, max_length=64)
    target_quantity: int = Field(default=1, ge=1, le=10_000_000)


class ReviewResolve(BaseModel):
    resolution: Literal["pass", "reject"]
    reviewer: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=2, max_length=500)
    corrected_fields: dict[str, str] = Field(default_factory=dict)


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class WorkstationCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    location: str = Field(default="", max_length=200)


class RuleVersionCreate(BaseModel):
    rules: dict[str, Any]
    change_reason: str = Field(min_length=3, max_length=500)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=200)
    display_name: str = Field(min_length=2, max_length=100)
    role: Literal["admin", "quality_engineer", "operator", "viewer"]

    _validate_password = field_validator("password")(strong_password)


class UserStatusUpdate(BaseModel):
    active: bool


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=8, max_length=200)
    new_password: str = Field(min_length=12, max_length=200)

    _validate_new_password = field_validator("new_password")(strong_password)


class OrderStatusUpdate(BaseModel):
    status: Literal["active", "completed", "cancelled"]

from __future__ import annotations

from pydantic import BaseModel, Field


class FieldMapping(BaseModel):
    order_no: str
    model_no: str | None = None
    customer: str | None = None
    product_name: str | None = None
    material: str | None = None
    product_type: str | None = None
    molding_method: str | None = None
    quantity: str
    weight_kg: str | None = None
    weight_ton: str | None = None
    due_date: str | None = None
    remark: str | None = None
    is_urgent: str | None = None


class ImportRequest(BaseModel):
    file_id: int
    mapping: FieldMapping
    replace_existing: bool = False


class OrderPayload(BaseModel):
    order_no: str = Field(min_length=1)
    model_no: str | None = ""
    customer: str | None = ""
    product_name: str | None = ""
    material: str | None = ""
    product_type: str
    molding_method: str | None = ""
    quantity: int = Field(ge=0)
    weight_ton: float = Field(ge=0)
    due_date: str | None = None
    status: str = "未开始"
    is_urgent: bool = False
    remark: str | None = ""


class CapacityPayload(BaseModel):
    process_name: str
    daily_capacity: float = Field(gt=0)
    unit: str
    remark: str | None = ""


class OverridePayload(BaseModel):
    date: str
    process_name: str
    capacity: float = Field(ge=0)
    unit: str
    reason: str | None = ""


class CheckinItem(BaseModel):
    task_id: int | None = None
    order_id: int
    process_name: str
    planned_qty: float = Field(ge=0)
    actual_qty: float = Field(ge=0)
    planned_units: int | None = Field(default=None, ge=0)
    actual_units: int | None = Field(default=None, ge=0)
    status: str
    reason: str | None = ""
    remark: str | None = ""


class CheckinRequest(BaseModel):
    date: str
    items: list[CheckinItem]
    idempotency_key: str | None = None


class PlanLockPayload(BaseModel):
    order_id: int
    process_name: str = "浇筑"
    locked_date: str
    planned_units: int | None = Field(default=None, ge=1)
    reason: str | None = ""


class PlanScopeLockPayload(BaseModel):
    scope_type: str
    scope_value: str
    process_name: str = "浇筑"
    reason: str | None = ""


class AiConfigPayload(BaseModel):
    enabled: bool = False
    apiKey: str | None = ""
    modelName: str | None = ""
    baseUrl: str | None = ""


class AiAskRequest(BaseModel):
    question: str

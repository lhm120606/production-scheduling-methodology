from __future__ import annotations

import json
import math
from datetime import date, timedelta
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "horizon_days": 30,
    "working_weekdays": [0, 1, 2, 3, 4, 5, 6],
    "molding_lead_days": 1,
    "casting_target_ton": 25.0,
    "casting_normal_min_ton": 23.0,
    "casting_normal_max_ton": 27.0,
    "casting_absolute_max_ton": 27.0,
    "qt_furnace_min_ton": 3.0,
    "qt_furnace_max_ton": 4.0,
}


def scheduler_config(conn) -> dict[str, Any]:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key='scheduler_config'"
    ).fetchone()
    if not row:
        return dict(DEFAULT_CONFIG)
    try:
        stored = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        stored = {}
    return {**DEFAULT_CONFIG, **stored, "working_weekdays": list(range(7))}


def is_production_day(day: date, config: dict[str, Any]) -> bool:
    return day.weekday() in set(config["working_weekdays"])


def production_days(start: date, count: int, config: dict[str, Any]) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if is_production_day(current, config):
            days.append(current)
        current += timedelta(days=1)
    return days


def shift_production_days(
    start: date,
    offset: int,
    config: dict[str, Any],
) -> date:
    if offset == 0:
        return start
    step = 1 if offset > 0 else -1
    remaining = abs(offset)
    current = start
    while remaining:
        current += timedelta(days=step)
        if is_production_day(current, config):
            remaining -= 1
    return current


def next_production_day(day: date, config: dict[str, Any]) -> date:
    current = day
    while not is_production_day(current, config):
        current += timedelta(days=1)
    return current


def opening_wait_hours(
    conn,
    unit_weight_kg: float | None,
    material: str | None,
) -> tuple[float | None, str | None]:
    if not unit_weight_kg or unit_weight_kg <= 0:
        return None, "缺少单件重量，无法查找开箱时间"
    row = conn.execute(
        """
        SELECT * FROM opening_rules
        WHERE ? BETWEEN min_weight_kg AND max_weight_kg
        ORDER BY max_weight_kg
        LIMIT 1
        """,
        (unit_weight_kg,),
    ).fetchone()
    if not row:
        return None, "单件重量超出开箱时间工作表范围"
    normalized_material = (material or "").replace(" ", "").upper()
    if normalized_material.startswith(("QT400", "QT500")):
        return float(row["min_hours"]), None
    return float(row["max_hours"]), None


def opening_date(
    casting_day: date,
    hours: float,
    config: dict[str, Any],
) -> date:
    calendar_days = max(1, math.ceil(hours / 24))
    return next_production_day(casting_day + timedelta(days=calendar_days), config)


def production_route(order: dict[str, Any]) -> tuple[list[str], str | None]:
    raw = order.get("route_json")
    if raw:
        try:
            route = json.loads(raw)
            if isinstance(route, list) and route:
                return [str(item) for item in route], None
        except (TypeError, json.JSONDecodeError):
            pass
    return ["打磨", "加工", "检验"], "工艺路线缺失，暂沿用历史可配置路线并需人工确认"

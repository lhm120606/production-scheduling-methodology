from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

from .db import get_conn, json_dumps, now_iso, rows_to_dicts
from .rules import (
    opening_date,
    opening_wait_hours,
    production_days,
    production_route,
    scheduler_config,
    shift_production_days,
)


PROCESSES = ["木模造型", "消失模造型", "浇筑", "开箱", "打磨", "加工", "检验"]
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
BUCKET_LABELS = {"large": "大件", "medium": "中件", "small": "小件"}
PROCESS_GROUPS = {
    "木模造型": "木模造型组",
    "消失模造型": "消失模造型组",
}


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def iso_week_label(day: date) -> str:
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def week_relation_label(target_start: date, current_start: date) -> str:
    offset = (target_start - current_start).days // 7
    if offset == 0:
        return "本周"
    if offset == 1:
        return "下周"
    if offset == 2:
        return "下下周"
    if offset > 2:
        return f"未来第{offset + 1}周"
    if offset == -1:
        return "上周"
    return f"过去第{abs(offset)}周"


def today_iso() -> str:
    return date.today().isoformat()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def chinese_month_day(day: date) -> str:
    return f"{day.month}月{day.day}日"


def deadline_alert_for_task(
    task: dict[str, Any],
    as_of: date,
) -> dict[str, Any] | None:
    process = str(task.get("process_name") or "")
    group_name = PROCESS_GROUPS.get(process)
    casting = parse_date(task.get("related_casting_date"))
    deadline = parse_date(
        task.get("latest_finish_date")
        or task.get("mandatory_finish_at")
        or task.get("date")
    )
    if not group_name or not casting or not deadline:
        return None

    days_to_casting = (casting - as_of).days
    days_to_deadline = (deadline - as_of).days
    if days_to_casting > 7 and days_to_deadline > 3:
        return None

    units = task.get("planned_units")
    weight = task.get("planned_weight_ton")
    quantity_text = (
        f"{int(units)}件"
        if units is not None
        else "该批次造型"
    )
    if weight is not None:
        quantity_text += f"（{float(weight):.2f}吨）"

    order_no = str(task.get("order_no") or "未编号订单")
    product_name = str(
        task.get("product_name")
        or task.get("product_type")
        or "铸件"
    )
    casting_label = chinese_month_day(casting)
    deadline_label = chinese_month_day(deadline)

    if days_to_deadline < 0:
        level = "overdue"
    elif days_to_deadline == 0:
        level = "urgent"
    else:
        level = (
            "urgent"
            if days_to_deadline <= 1 or days_to_casting <= 2
            else "warning"
            if days_to_deadline <= 3 or days_to_casting <= 5
            else "notice"
        )

    action = "请立即完成" if days_to_deadline < 0 else "须完成"
    message = (
        f"{group_name}{action}订单 {order_no} 的{product_name}{process}任务"
        f"{quantity_text}；最晚完成时间为{deadline_label}，"
        f"计划浇筑时间为{casting_label}。"
    )
    return {
        "task_id": task.get("id"),
        "batch_key": task.get("batch_key"),
        "order_id": task.get("order_id"),
        "order_no": order_no,
        "product_name": product_name,
        "process_name": process,
        "group_name": group_name,
        "planned_units": units,
        "planned_weight_ton": weight,
        "deadline_date": deadline.isoformat(),
        "casting_date": casting.isoformat(),
        "days_to_deadline": days_to_deadline,
        "days_to_casting": days_to_casting,
        "level": level,
        "message": message,
    }


def build_deadline_alerts(
    tasks: list[dict[str, Any]],
    as_of: date,
) -> list[dict[str, Any]]:
    alerts = [
        alert
        for task in tasks
        if (alert := deadline_alert_for_task(task, as_of)) is not None
    ]
    level_rank = {"overdue": 0, "urgent": 1, "warning": 2, "notice": 3}
    alerts.sort(
        key=lambda alert: (
            level_rank.get(alert["level"], 9),
            alert["deadline_date"],
            alert["casting_date"],
            alert["order_no"],
        )
    )
    return alerts


def get_capacities(conn, target: date) -> dict[str, dict[str, Any]]:
    defaults = {
        row["process_name"]: dict(row)
        for row in conn.execute("SELECT * FROM capacity_config")
    }
    overrides = {
        row["process_name"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM capacity_overrides WHERE date = ?",
            (target.isoformat(),),
        )
    }
    result: dict[str, dict[str, Any]] = {}
    for process, row in defaults.items():
        override = overrides.get(process)
        result[process] = {
            "process_name": process,
            "capacity": float(
                override["capacity"] if override else row["daily_capacity"]
            ),
            "unit": override["unit"] if override else row["unit"],
            "is_override": bool(override),
        }
    return result


def default_capacities(conn) -> dict[str, float]:
    return {
        row["process_name"]: float(row["daily_capacity"])
        for row in conn.execute(
            "SELECT process_name, daily_capacity FROM capacity_config"
        )
    }


def single_weight_ton(order: dict[str, Any]) -> float | None:
    unit_kg = float(order.get("unit_weight_kg") or 0)
    if unit_kg > 0:
        return unit_kg / 1000
    quantity = float(order.get("quantity") or 0)
    weight_ton = float(order.get("weight_ton") or 0)
    if quantity <= 0 or weight_ton <= 0:
        return None
    return weight_ton / quantity


def casting_bucket(order: dict[str, Any]) -> str:
    unit_ton = single_weight_ton(order) or 0
    if unit_ton >= 5:
        return "large"
    if unit_ton >= 2:
        return "medium"
    return "small"


def molding_process(order: dict[str, Any]) -> str | None:
    text = " ".join(
        str(order.get(key) or "")
        for key in ("molding_method", "product_type")
    )
    if "消失" in text or "泡沫" in text:
        return "消失模造型"
    if any(word in text for word in ("木", "抬模")):
        return "木模造型"
    return None


def order_priority(
    order: dict[str, Any],
    progress: list[dict[str, Any]],
    capacities: dict[str, float],
    base: date,
) -> dict[str, Any]:
    remaining_days = 0.0
    for item in progress:
        cap = capacities.get(item["process_name"], 0) or 1
        remaining_days += float(item["remaining_qty"]) / cap
    due = parse_date(order.get("due_date"))
    days_to_due = (due - base).days if due else 999
    buffer_days = days_to_due - remaining_days
    if buffer_days < 0:
        level = "P0"
    elif buffer_days <= 3:
        level = "P1"
    elif buffer_days <= 7:
        level = "P2"
    else:
        level = "P3"
    if order.get("is_urgent") and level != "P0":
        level = {"P3": "P2", "P2": "P1", "P1": "P0"}.get(level, level)
    return {
        "priority_level": level,
        "remaining_work_days": round(remaining_days, 2),
        "days_to_due": days_to_due,
        "buffer_days": round(buffer_days, 2),
        "is_overdue": bool(due and due < base),
    }


def add_issue(
    conn,
    version: str,
    order: dict[str, Any] | None,
    code: str,
    message: str,
    severity: str = "警告",
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO data_issues
        (schedule_version, order_id, order_no, issue_code, severity, message, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, '待确认', ?)
        """,
        (
            version,
            order.get("id") if order else None,
            order.get("order_no") if order else None,
            code,
            severity,
            message,
            now_iso(),
        ),
    )


def reverse_process_deadlines(
    conn,
    order: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, date]:
    """Build each process's latest finish date backwards from delivery."""
    due = parse_date(order.get("due_date"))
    if not due:
        return {}
    route, _ = production_route(order)
    deadlines: dict[str, date] = {}
    cursor = due
    for process in reversed(route):
        deadlines[process] = cursor
        cursor = shift_production_days(cursor, -1, config)
    deadlines["开箱"] = cursor
    unit_ton = single_weight_ton(order)
    hours, _ = opening_wait_hours(
        conn,
        unit_ton * 1000 if unit_ton else None,
        order.get("material"),
    )
    wait_days = max(1, math.ceil(float(hours or 24) / 24))
    casting_deadline = cursor - timedelta(days=wait_days)
    working_days = set(config["working_weekdays"])
    while casting_deadline.weekday() not in working_days:
        casting_deadline -= timedelta(days=1)
    deadlines["浇筑"] = casting_deadline
    method = molding_process(order)
    if method:
        deadlines[method] = shift_production_days(
            casting_deadline,
            -int(config["molding_lead_days"]),
            config,
        )
    return deadlines


def task_row(
    *,
    version: str,
    day: date,
    order: dict[str, Any],
    process: str,
    units: int | None,
    weight_ton: float | None,
    workload_qty: float,
    workload_unit: str,
    priority: str,
    related_casting: date | None,
    estimated_hours: float | None,
    reason: str,
    risk: str | None = None,
    locked: bool = False,
    batch_key: str | None = None,
    task_nature: str | None = None,
    latest_finish: date | None = None,
) -> dict[str, Any]:
    issue = None
    if units is None:
        issue = "计划件数缺失"
    elif weight_ton is None:
        issue = "缺少单件重量，无法计算吨位"
    week_start, week_end = week_bounds(day)
    cross_week = bool(
        related_casting and week_bounds(related_casting)[0] != week_start
    )
    current_week_start, current_week_end = week_bounds(date.today())
    due = parse_date(order.get("due_date"))
    if latest_finish and day > latest_finish:
        risk = risk or f"超过交期倒排节点{latest_finish.isoformat()}"
    if not task_nature:
        if locked:
            task_nature = "锁定任务"
        elif order.get("is_urgent"):
            task_nature = "急单"
        elif (
            week_start > current_week_start
            and due
            and due <= current_week_end
        ):
            task_nature = "上周顺延任务"
            risk = risk or "本周产能不足，顺延至下周"
        elif risk:
            task_nature = "待确认任务"
        elif (
            process.endswith("造型")
            and related_casting
            and week_bounds(related_casting)[0]
            == week_start + timedelta(days=7)
        ):
            task_nature = "下周浇筑前置任务"
        elif process.endswith("造型"):
            task_nature = "本周必须完成"
        else:
            task_nature = "本周正常任务"
    return {
        "schedule_version": version,
        "date": day.isoformat(),
        "order_id": order["id"],
        "order_no": order["order_no"],
        "contract_no": order.get("contract_no"),
        "model_no": order.get("model_no"),
        "customer": order.get("customer"),
        "product_name": order.get("product_name"),
        "material": order.get("material"),
        "product_type": order.get("product_type"),
        "process_name": process,
        "planned_qty": round(workload_qty, 6),
        "unit": workload_unit,
        "priority_level": priority,
        "due_date": order.get("due_date"),
        "status": "计划",
        "rolled_over": 0,
        "is_urgent": int(order.get("is_urgent") or 0),
        "planned_units": units,
        "planned_weight_ton": round(weight_ton, 6) if weight_ton is not None else None,
        "unit_weight_kg": order.get("unit_weight_kg"),
        "order_quantity": int(round(float(order.get("quantity") or 0))),
        "completed_units_before_plan": int(order.get("_completed_units_before_plan") or 0),
        "order_remark": order.get("remark"),
        "estimated_hours": round(estimated_hours, 2) if estimated_hours is not None else None,
        "quantity_issue": issue,
        "related_casting_date": related_casting.isoformat() if related_casting else None,
        "mandatory_finish_at": day.isoformat() if process.endswith("造型") else None,
        "decision_reason": reason,
        "risk_status": risk,
        "is_locked": int(locked),
        "batch_key": batch_key,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "iso_week": iso_week_label(day),
        "task_nature": task_nature,
        "latest_finish_date": (
            latest_finish.isoformat() if latest_finish
            else day.isoformat() if process.endswith("造型")
            else order.get("due_date")
        ),
        "is_cross_week": int(cross_week),
        "created_at": now_iso(),
    }


def insert_tasks(conn, tasks: list[dict[str, Any]]) -> None:
    columns = [
        "schedule_version", "date", "order_id", "order_no", "contract_no", "model_no",
        "customer", "product_name", "material", "product_type", "process_name",
        "planned_qty", "unit", "priority_level",
        "due_date", "status", "rolled_over", "is_urgent", "planned_units",
        "planned_weight_ton", "unit_weight_kg", "order_quantity",
        "completed_units_before_plan", "order_remark", "estimated_hours", "quantity_issue",
        "related_casting_date", "mandatory_finish_at", "decision_reason",
        "risk_status", "is_locked", "batch_key", "week_start", "week_end",
        "iso_week", "task_nature", "latest_finish_date", "is_cross_week",
        "created_at",
    ]
    sql = (
        f"INSERT INTO schedule_tasks ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})"
    )
    conn.executemany(sql, [[task.get(column) for column in columns] for task in tasks])


def decorate_task_identifiers(rows: list[dict[str, Any]]) -> None:
    """Add shop-floor batch and piece-range labels without inventing order data."""
    order_group: dict[int, str] = {}
    group_total: dict[str, int] = defaultdict(int)
    group_completed: dict[str, int] = defaultdict(int)
    seen_orders: set[int] = set()
    for row in rows:
        order_no = str(row.get("order_no") or "")
        model_no = str(row.get("model_no") or "").strip()
        if not model_no:
            base, marker, suffix = order_no.rpartition("-R")
            model_no = base if marker and suffix.isdigit() else order_no
            contract_no = str(row.get("contract_no") or "").strip()
            if contract_no and model_no.startswith(f"{contract_no}-"):
                model_no = model_no[len(contract_no) + 1:]
        row["model_no"] = model_no or order_no or "-"
        contract_no = str(row.get("contract_no") or "").strip()
        if contract_no:
            key = f"合同:{contract_no}|型号:{row['model_no']}"
        else:
            key = (
                f"客户:{str(row.get('customer') or '').strip()}|"
                f"型号:{row['model_no']}|交期:{row.get('due_date') or ''}"
            )
        order_id = int(row.get("order_id") or 0)
        order_group[order_id] = key
        row["piece_group_key"] = key
        if order_id not in seen_orders:
            seen_orders.add(order_id)
            group_total[key] += int(row.get("order_quantity") or row.get("planned_units") or 0)
            group_completed[key] += int(row.get("completed_units_before_plan") or 0)

    casting_groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("process_name") != "浇筑" or not row.get("batch_key"):
            continue
        group = casting_groups.setdefault(
            str(row["batch_key"]),
            {
                "rows": [], "units": 0, "date": row["date"],
                "order_id": row["order_id"],
                "piece_group_key": order_group.get(int(row["order_id"])),
            },
        )
        group["rows"].append(row)
        group["units"] += int(row.get("planned_units") or 0)

    piece_group_cursor: dict[str, int] = dict(group_completed)
    day_sequence: dict[str, int] = defaultdict(int)
    batch_info: dict[str, dict[str, Any]] = {}
    ordered_groups = sorted(
        casting_groups.items(),
        key=lambda item: (
            item[1]["date"],
            min(PRIORITY_RANK.get(row.get("priority_level"), 9) for row in item[1]["rows"]),
            item[1]["order_id"],
            item[0],
        ),
    )
    for batch_key, group in ordered_groups:
        piece_group_key = str(group["piece_group_key"])
        start = piece_group_cursor.get(piece_group_key, 0) + 1
        end = piece_group_cursor.get(piece_group_key, 0) + int(group["units"])
        piece_group_cursor[piece_group_key] = end
        casting_date = str(group["date"])
        day_sequence[casting_date] += 1
        batch_info[batch_key] = {
            "piece_start": start,
            "piece_end": end,
            "piece_group_key": piece_group_key,
            "production_batch_no": (
                f"JZ{casting_date[2:4]}{casting_date[5:7]}{casting_date[8:10]}-"
                f"{day_sequence[casting_date]:02d}"
            ),
        }

    process_batch_units: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        batch_key = str(row.get("batch_key") or "")
        if batch_key:
            process_batch_units[(str(row.get("process_name") or ""), batch_key)] += int(
                row.get("planned_units") or 0
            )
    segment_offset: dict[tuple[str, str], int] = defaultdict(int)
    for row in sorted(
        rows,
        key=lambda item: (
            item.get("process_name") or "",
            item.get("batch_key") or "",
            item.get("date") or "",
            item.get("id") or 0,
        ),
    ):
        units = int(row.get("planned_units") or 0)
        piece_group_key = order_group.get(int(row.get("order_id") or 0), "")
        total = int(group_total.get(piece_group_key) or row.get("order_quantity") or units)
        batch_key = str(row.get("batch_key") or "")
        info = batch_info.get(batch_key)
        if info:
            offset_key = (str(row.get("process_name") or ""), batch_key)
            process_units = process_batch_units[offset_key]
            batch_units = int(info["piece_end"]) - int(info["piece_start"]) + 1
            process_start = int(info["piece_start"])
            if str(row.get("process_name") or "").endswith("造型") and process_units < batch_units:
                process_start = int(info["piece_end"]) - process_units + 1
            start = process_start + segment_offset[offset_key]
            end = start + units - 1 if units > 0 else start - 1
            end = min(end, int(info["piece_end"]))
            segment_offset[offset_key] += units
            row["production_batch_no"] = info["production_batch_no"]
        else:
            start = int(group_completed.get(piece_group_key) or 0) + 1
            end = start + units - 1 if units > 0 else start - 1
            casting_date = str(row.get("related_casting_date") or row.get("date") or "")
            row["production_batch_no"] = f"待排-{casting_date[5:10]}" if casting_date else "待排"
        row["piece_start"] = start if units > 0 else None
        row["piece_end"] = end if units > 0 else None
        row["piece_total"] = total
        if units <= 0:
            row["piece_label"] = "件号待确认"
        elif start == end:
            row["piece_label"] = f"第{start}件 / 共{total}件"
        else:
            row["piece_label"] = f"第{start}–{end}件 / 共{total}件"
        row["display_product_name"] = (
            f"{row.get('product_name') or row.get('product_type') or '未命名铸件'}"
            f"（{row['piece_label']}）"
        )
        row["total_weight_kg"] = (
            round(float(row["planned_weight_ton"]) * 1000, 2)
            if row.get("planned_weight_ton") is not None
            else None
        )
        row["remaining_order_units"] = max(total - end, 0) if units > 0 else total


def shop_floor_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("date") or "",
        PROCESSES.index(row["process_name"]) if row.get("process_name") in PROCESSES else 99,
        PRIORITY_RANK.get(row.get("priority_level"), 9),
        row.get("order_id") or 0,
        row.get("piece_start") or 0,
        row.get("id") or 0,
    )


def build_week_plan_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse internal day-level tasks into a readable weekly shop plan."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for task in rows:
        group_key = str(task.get("order_id") or task.get("order_no"))
        process = str(task.get("process_name") or "待确认")
        key = (group_key, process)
        row = groups.get(key)
        if row is None:
            row = dict(task)
            row.update(
                {
                    "planned_units": 0,
                    "planned_weight_ton": 0.0,
                    "total_weight_kg": 0.0,
                    "piece_start": None,
                    "piece_end": None,
                    "remaining_order_units": task.get("piece_total"),
                    "task_count": 0,
                    "_batch_numbers": [],
                    "_order_numbers": [],
                    "_risk_statuses": [],
                    "_remarks": [],
                    "_has_weight": False,
                    "_latest_dates": [],
                    "_due_dates": [],
                }
            )
            groups[key] = row

        units = int(task.get("planned_units") or 0)
        row["planned_units"] += units
        row["task_count"] += 1
        if task.get("planned_weight_ton") is not None:
            row["_has_weight"] = True
            row["planned_weight_ton"] += float(task["planned_weight_ton"])
            row["total_weight_kg"] += float(task["planned_weight_ton"]) * 1000
        for target, value in (
            ("_batch_numbers", task.get("production_batch_no")),
            ("_order_numbers", task.get("order_no")),
            ("_risk_statuses", task.get("risk_status")),
            ("_remarks", task.get("order_remark")),
            ("_latest_dates", task.get("latest_finish_date") or task.get("mandatory_finish_at")),
            ("_due_dates", task.get("due_date")),
        ):
            if value and value not in row[target]:
                row[target].append(value)
        start = task.get("piece_start")
        end = task.get("piece_end")
        if start is not None:
            row["piece_start"] = min(row["piece_start"], start) if row["piece_start"] is not None else start
        if end is not None:
            row["piece_end"] = max(row["piece_end"], end) if row["piece_end"] is not None else end
        if task.get("remaining_order_units") is not None:
            row["remaining_order_units"] = min(
                int(row["remaining_order_units"] or 0),
                int(task["remaining_order_units"]),
            )

    result: list[dict[str, Any]] = []
    for row in groups.values():
        total = int(row.get("piece_total") or row.get("order_quantity") or row["planned_units"])
        start = row.get("piece_start")
        end = row.get("piece_end")
        if start is None or end is None:
            piece_label = "件号待确认"
        elif start == end:
            piece_label = f"第{start}件 / 共{total}件"
        else:
            piece_label = f"第{start}–{end}件 / 共{total}件"
        row["piece_label"] = piece_label
        row["display_product_name"] = (
            f"{row.get('product_name') or row.get('product_type') or '未命名铸件'}"
            f"（{piece_label}）"
        )
        batches = row.pop("_batch_numbers")
        row["production_batch_no"] = (
            "、".join(batches[:2]) + (f" 等{len(batches)}批" if len(batches) > 2 else "")
            if batches else "待排"
        )
        orders = row.pop("_order_numbers")
        row["order_no"] = (
            orders[0] + (f" 等{len(orders)}单" if len(orders) > 1 else "")
            if orders else str(row.get("order_no") or "-")
        )
        risks = row.pop("_risk_statuses")
        remarks = row.pop("_remarks")
        row["risk_status"] = "；".join(risks)
        row["order_remark"] = "；".join(remarks)
        latest_dates = row.pop("_latest_dates")
        due_dates = row.pop("_due_dates")
        row["latest_finish_date"] = min(latest_dates) if latest_dates else None
        row["due_date"] = min(due_dates) if due_dates else None
        if not row.pop("_has_weight"):
            row["planned_weight_ton"] = None
            row["total_weight_kg"] = None
        else:
            row["planned_weight_ton"] = round(float(row["planned_weight_ton"]), 3)
            row["total_weight_kg"] = round(float(row["total_weight_kg"]), 2)
        result.append(row)

    process_rank = {name: index for index, name in enumerate(PROCESSES)}
    result.sort(
        key=lambda row: (
            process_rank.get(str(row.get("process_name")), 99),
            PRIORITY_RANK.get(str(row.get("priority_level")), 9),
            row.get("due_date") or "9999-12-31",
            row.get("model_no") or "",
        )
    )
    return result


def load_states(conn, base: date, version: str) -> list[dict[str, Any]]:
    caps = default_capacities(conn)
    config = scheduler_config(conn)
    orders = rows_to_dicts(
        conn.execute(
            """
            SELECT * FROM orders
            WHERE status NOT IN ('已完成', '暂停')
            ORDER BY is_urgent DESC, due_date IS NULL, due_date, id
            """
        ).fetchall()
    )
    locks = {
        int(row["order_id"]): dict(row)
        for row in conn.execute(
            "SELECT * FROM plan_locks WHERE process_name='浇筑'"
        ).fetchall()
    }
    states: list[dict[str, Any]] = []
    for order in orders:
        progress = rows_to_dicts(
            conn.execute(
                "SELECT * FROM order_progress WHERE order_id=?",
                (order["id"],),
            ).fetchall()
        )
        by_process = {row["process_name"]: row for row in progress}
        priority = order_priority(order, progress, caps, base)
        quantity = int(round(float(order.get("quantity") or 0)))
        unit_ton = single_weight_ton(order)
        if quantity <= 0:
            add_issue(conn, version, order, "PLAN_UNITS_MISSING", "计划件数缺失", "阻断")
            continue
        if not unit_ton:
            add_issue(
                conn,
                version,
                order,
                "UNIT_WEIGHT_MISSING",
                "缺少单件重量，无法计算吨位并安排浇筑",
                "阻断",
            )
            continue
        if not order.get("material"):
            add_issue(conn, version, order, "MATERIAL_MISSING", "材质缺失，无法确认炉次组合")
        method = molding_process(order)
        if not method:
            add_issue(conn, version, order, "MOLDING_METHOD_MISSING", "造型方式无法识别", "阻断")
            continue
        casting_progress = by_process.get("浇筑")
        molding_progress = by_process.get(method)
        casting_remaining_ton = float(
            casting_progress["remaining_qty"] if casting_progress else order["weight_ton"]
        )
        casting_units = min(
            quantity,
            max(0, math.ceil(casting_remaining_ton / unit_ton - 1e-9)),
        )
        casting_completed_ton = max(
            float(order["weight_ton"] or 0) - casting_remaining_ton,
            0,
        )
        molding_completed_ton = float(
            molding_progress["completed_qty"] if molding_progress else 0
        )
        available_molded_units = max(
            0,
            math.floor(
                (molding_completed_ton - casting_completed_ton + 1e-9) / unit_ton
            ),
        )
        if casting_units <= 0:
            continue
        order["_completed_units_before_plan"] = max(quantity - casting_units, 0)
        deadlines = reverse_process_deadlines(conn, order, config)
        casting_days = max(
            1,
            math.ceil(
                casting_units * unit_ton
                / float(config["casting_target_ton"])
            ),
        )
        latest_casting = deadlines.get("浇筑")
        casting_window_start = (
            shift_production_days(latest_casting, -(casting_days - 1), config)
            if latest_casting else base
        )
        if casting_window_start < base:
            casting_window_start = base
        states.append(
            {
                "order": order,
                "priority": priority,
                "remaining_units": casting_units,
                "available_molded_units": available_molded_units,
                "unit_ton": unit_ton,
                "molding_process": method,
                "bucket": casting_bucket(order),
                "lock": locks.get(int(order["id"])),
                "deadlines": deadlines,
                "casting_window_start": casting_window_start,
                "wait_days": max((base - (parse_date(order.get("due_date")) or base)).days, 0),
            }
        )
    return states


def state_sort_key(
    state: dict[str, Any],
    bucket_ton: dict[str, float],
    day_total: float,
) -> tuple[Any, ...]:
    due = parse_date(state["order"].get("due_date")) or date.max
    latest_casting = state.get("deadlines", {}).get("浇筑") or date.max
    bucket = state["bucket"]
    targets = {"large": 0.25, "medium": 0.50, "small": 0.25}
    current_share = bucket_ton[bucket] / day_total if day_total else 0
    structure_penalty = 0 if current_share < targets[bucket] else 1
    return (
        0 if state["lock"] else 1,
        0 if state["priority"]["is_overdue"] else 1,
        0 if state["order"].get("is_urgent") else 1,
        PRIORITY_RANK.get(state["priority"]["priority_level"], 9),
        latest_casting,
        due,
        0 if state["available_molded_units"] > 0 else 1,
        structure_penalty,
        -state["wait_days"],
        state["order"]["id"],
    )


def reserve_molding(
    conn,
    state: dict[str, Any],
    units: int,
    casting_day: date,
    base: date,
    config: dict[str, Any],
    remaining_capacity: dict[tuple[str, str], float],
) -> tuple[list[tuple[date, int]], int] | None:
    ready_units = min(units, state["available_molded_units"])
    need_units = units - ready_units
    if need_units <= 0:
        return [], ready_units
    last_day = shift_production_days(
        casting_day,
        -int(config["molding_lead_days"]),
        config,
    )
    if last_day < base:
        return None
    candidates = production_days(base, 1, config)
    if not candidates:
        return None
    days: list[date] = []
    current = candidates[0]
    while current <= last_day:
        if current.weekday() in set(config["working_weekdays"]):
            days.append(current)
        current += timedelta(days=1)
    allocations: list[tuple[date, int]] = []
    remaining = need_units
    for mold_day in reversed(days):
        key = (mold_day.isoformat(), state["molding_process"])
        if key not in remaining_capacity:
            capacity = get_capacities(conn, mold_day).get(
                state["molding_process"],
                {"capacity": 0},
            )["capacity"]
            remaining_capacity[key] = float(capacity)
        pieces = min(
            remaining,
            math.floor(
                (remaining_capacity[key] + 1e-9) / state["unit_ton"]
            ),
        )
        if pieces > 0:
            allocations.append((mold_day, pieces))
            remaining -= pieces
            remaining_capacity[key] -= pieces * state["unit_ton"]
        if remaining <= 0:
            break
    if remaining > 0:
        for mold_day, pieces in allocations:
            key = (mold_day.isoformat(), state["molding_process"])
            remaining_capacity[key] += pieces * state["unit_ton"]
        return None
    return list(reversed(allocations)), ready_units


def choose_batch_units(
    state: dict[str, Any],
    capacity_left: float,
    config: dict[str, Any],
) -> int:
    unit_ton = state["unit_ton"]
    fit = math.floor((capacity_left + 1e-9) / unit_ton)
    if fit <= 0:
        return 0
    units = min(state["remaining_units"], fit)
    material = str(state["order"].get("material") or "").upper().replace(" ", "")
    if material.startswith("QT"):
        qt_max = float(config["qt_furnace_max_ton"])
        max_qt_units = max(1, math.floor(qt_max / unit_ton))
        units = min(units, max_qt_units)
    return max(units, 0)


def schedule_post_processes(
    conn,
    version: str,
    casting_tasks: list[dict[str, Any]],
    order_map: dict[int, dict[str, Any]],
    config: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> None:
    post_remaining: dict[tuple[str, str], float] = {}
    for casting in casting_tasks:
        order = order_map[casting["order_id"]]
        deadlines = reverse_process_deadlines(conn, order, config)
        units = int(casting["planned_units"] or 0)
        if units <= 0:
            add_issue(conn, version, order, "PLAN_UNITS_MISSING", "浇筑批次缺少计划件数", "阻断")
            continue
        unit_ton = single_weight_ton(order)
        hours, issue = opening_wait_hours(
            conn,
            (unit_ton * 1000) if unit_ton else None,
            order.get("material"),
        )
        if issue or hours is None:
            add_issue(conn, version, order, "OPENING_RULE_MISSING", issue or "缺少开箱时间规则", "阻断")
            continue
        casting_day = parse_date(casting["date"]) or date.today()
        open_day = opening_date(casting_day, hours, config)
        tasks.append(
            task_row(
                version=version,
                day=open_day,
                order=order,
                process="开箱",
                units=units,
                weight_ton=casting["planned_weight_ton"],
                workload_qty=hours,
                workload_unit="小时",
                priority=casting["priority_level"],
                related_casting=casting_day,
                estimated_hours=hours,
                reason=f"浇筑后按开箱时间工作表等待{hours:g}小时",
                risk=casting.get("risk_status"),
                batch_key=casting["batch_key"],
                latest_finish=deadlines.get("开箱"),
            )
        )
        route, route_issue = production_route(order)
        if route_issue:
            add_issue(conn, version, order, "ROUTE_MISSING", route_issue)
        cursor = shift_production_days(open_day, 1, config)
        for process in route:
            remaining_units = units
            while remaining_units > 0:
                key = (cursor.isoformat(), process)
                if key not in post_remaining:
                    post_remaining[key] = float(
                        get_capacities(conn, cursor).get(
                            process,
                            {"capacity": remaining_units},
                        )["capacity"]
                    )
                planned_units = min(
                    remaining_units,
                    max(math.floor(post_remaining[key] + 1e-9), 0),
                )
                if planned_units <= 0:
                    cursor = shift_production_days(cursor, 1, config)
                    continue
                batch_weight = (
                    planned_units * unit_ton if unit_ton is not None else None
                )
                tasks.append(
                    task_row(
                        version=version,
                        day=cursor,
                        order=order,
                        process=process,
                        units=planned_units,
                        weight_ton=batch_weight,
                        workload_qty=float(planned_units),
                        workload_unit="件",
                        priority=casting["priority_level"],
                        related_casting=casting_day,
                        estimated_hours=None,
                        reason=f"承接{open_day.isoformat()}开箱批次，按{process}日产能滚动",
                        risk=route_issue,
                        batch_key=casting["batch_key"],
                        latest_finish=deadlines.get(process),
                    )
                )
                post_remaining[key] -= planned_units
                remaining_units -= planned_units
                if remaining_units > 0:
                    cursor = shift_production_days(cursor, 1, config)
            cursor = shift_production_days(cursor, 1, config)


def record_adjustments(
    conn,
    version: str,
    previous_version: str | None,
) -> int:
    if not previous_version:
        return 0
    before = rows_to_dicts(
        conn.execute(
            """
            SELECT order_id, order_no, process_name, MIN(date) AS date, SUM(planned_units) AS units
            FROM schedule_tasks WHERE schedule_version=?
            GROUP BY order_id, process_name
            """,
            (previous_version,),
        ).fetchall()
    )
    after = rows_to_dicts(
        conn.execute(
            """
            SELECT order_id, order_no, process_name, MIN(date) AS date, SUM(planned_units) AS units
            FROM schedule_tasks WHERE schedule_version=?
            GROUP BY order_id, process_name
            """,
            (version,),
        ).fetchall()
    )
    before_map = {(row["order_id"], row["process_name"]): row for row in before}
    after_map = {(row["order_id"], row["process_name"]): row for row in after}
    count = 0
    urgent_trigger = conn.execute(
        "SELECT 1 FROM orders WHERE is_urgent=1 LIMIT 1"
    ).fetchone()
    for key in sorted(set(before_map) | set(after_map)):
        old = before_map.get(key)
        row = after_map.get(key)
        before_date = old["date"] if old else None
        after_date = row["date"] if row else None
        before_units = int(old["units"] or 0) if old else 0
        after_units = int(row["units"] or 0) if row else 0
        if before_date == after_date and before_units == after_units:
            continue
        conn.execute(
            """
            INSERT INTO schedule_adjustments
            (schedule_version, previous_version, order_id, order_no, process_name,
             before_date, after_date, before_units, after_units, planned_units,
             reason, triggered_by_urgent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version,
                previous_version,
                (row or old)["order_id"],
                (row or old)["order_no"],
                (row or old)["process_name"],
                before_date,
                after_date,
                before_units,
                after_units,
                after_units,
                (
                    "任务未消失，已进入异常或顺延清单"
                    if row is None
                    else "根据最新完成量、产能、锁定和前置可行性重新计算"
                ),
                int(bool(urgent_trigger)),
                now_iso(),
            ),
        )
        count += 1
    return count


def scope_locked_casting_tasks(
    conn,
    previous_version: str | None,
) -> list[dict[str, Any]]:
    if not previous_version:
        return []
    locks = rows_to_dicts(
        conn.execute(
            "SELECT * FROM plan_scope_locks ORDER BY id"
        ).fetchall()
    )
    if not locks:
        return []
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT * FROM schedule_tasks
            WHERE schedule_version=? AND process_name='浇筑'
            ORDER BY date, order_id, id
            """,
            (previous_version,),
        ).fetchall()
    )
    matched: dict[int, dict[str, Any]] = {}
    for lock in locks:
        source_version = lock.get("source_schedule_version") or previous_version
        source_rows = rows
        if source_version != previous_version:
            source_rows = rows_to_dicts(
                conn.execute(
                    """
                    SELECT * FROM schedule_tasks
                    WHERE schedule_version=? AND process_name='浇筑'
                    ORDER BY date, order_id, id
                    """,
                    (source_version,),
                ).fetchall()
            )
        scope_type = lock["scope_type"]
        scope_value = lock["scope_value"]
        for row in source_rows:
            is_match = (
                (scope_type == "week" and row.get("week_start") == scope_value)
                or (scope_type == "day" and row["date"] == scope_value)
                or (scope_type == "order" and str(row["order_id"]) == scope_value)
                or (scope_type == "batch" and row.get("batch_key") == scope_value)
            )
            if is_match:
                matched[int(row["id"])] = row
    return sorted(
        matched.values(),
        key=lambda row: (row["date"], row["order_id"], row["id"]),
    )


def generate_schedule(trigger_type: str = "manual") -> dict[str, Any]:
    base = date.today()
    version = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}"
    with get_conn() as conn:
        config = scheduler_config(conn)
        previous_version = latest_version(conn)
        states = load_states(conn, base, version)
        order_map = {state["order"]["id"]: state["order"] for state in states}
        horizon = production_days(base, int(config["horizon_days"]), config)
        molding_capacity: dict[tuple[str, str], float] = {}
        tasks: list[dict[str, Any]] = []
        casting_tasks: list[dict[str, Any]] = []
        unplanned_reasons: dict[int, str] = {}
        forced_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for old_task in scope_locked_casting_tasks(conn, previous_version):
            forced_by_day[old_task["date"]].append(old_task)
        state_by_order = {
            int(state["order"]["id"]): state for state in states
        }

        for casting_day in horizon:
            capacity_data = get_capacities(conn, casting_day).get(
                "浇筑",
                {"capacity": config["casting_target_ton"], "is_override": False},
            )
            configured = float(capacity_data["capacity"])
            if capacity_data.get("is_override"):
                day_limit = configured
            else:
                day_limit = min(
                    max(configured, float(config["casting_target_ton"])),
                    float(config["casting_absolute_max_ton"]),
                )
            day_total = 0.0
            bucket_ton = defaultdict(float)
            for old_task in forced_by_day.get(casting_day.isoformat(), []):
                state = state_by_order.get(int(old_task["order_id"]))
                if not state or state["remaining_units"] <= 0:
                    continue
                units = min(
                    int(old_task.get("planned_units") or 0),
                    int(state["remaining_units"]),
                )
                if units <= 0:
                    continue
                reservation = reserve_molding(
                    conn,
                    state,
                    units,
                    casting_day,
                    base,
                    config,
                    molding_capacity,
                )
                if reservation is None:
                    add_issue(
                        conn,
                        version,
                        state["order"],
                        "LOCKED_PLAN_CONFLICT",
                        f"锁定浇筑任务{casting_day.isoformat()}与当前造型能力冲突，已保留并需人工确认",
                        "阻断",
                    )
                    allocations, ready_units = [], min(
                        units, state["available_molded_units"]
                    )
                else:
                    allocations, ready_units = reservation
                batch_weight = round(units * state["unit_ton"], 6)
                batch_key = old_task.get("batch_key") or (
                    f"{state['order']['id']}-{casting_day.isoformat()}-locked"
                )
                for mold_day, mold_units in allocations:
                    mold_weight = round(
                        mold_units * state["unit_ton"], 6
                    )
                    tasks.append(
                        task_row(
                            version=version,
                            day=mold_day,
                            order=state["order"],
                            process=state["molding_process"],
                            units=mold_units,
                            weight_ton=mold_weight,
                            workload_qty=mold_weight,
                            workload_unit="吨",
                            priority=state["priority"]["priority_level"],
                            related_casting=casting_day,
                            estimated_hours=None,
                            reason=f"锁定批次为{casting_day.isoformat()}浇筑，反推造型",
                            locked=True,
                            batch_key=batch_key,
                            task_nature="锁定任务",
                            latest_finish=state["deadlines"].get(state["molding_process"]),
                        )
                    )
                casting_task = task_row(
                    version=version,
                    day=casting_day,
                    order=state["order"],
                    process="浇筑",
                    units=units,
                    weight_ton=batch_weight,
                    workload_qty=batch_weight,
                    workload_unit="吨",
                    priority=state["priority"]["priority_level"],
                    related_casting=casting_day,
                    estimated_hours=None,
                    reason="按周/日/订单/批次锁定保留原浇筑日期",
                    risk=(
                        "锁定计划超过当前浇筑产能，需人工确认"
                        if day_total + batch_weight > day_limit + 1e-9
                        else None
                    ),
                    locked=True,
                    batch_key=batch_key,
                    task_nature="锁定任务",
                    latest_finish=state["deadlines"].get("浇筑"),
                )
                tasks.append(casting_task)
                casting_tasks.append(casting_task)
                day_total += batch_weight
                bucket_ton[state["bucket"]] += batch_weight
                state["remaining_units"] -= units
                state["available_molded_units"] = max(
                    state["available_molded_units"] - ready_units, 0
                )
            made_progress = True
            while made_progress and day_total < day_limit - 1e-9:
                made_progress = False
                candidates = []
                for state in states:
                    if state["remaining_units"] <= 0:
                        continue
                    lock = state["lock"]
                    if not lock and casting_day < state["casting_window_start"]:
                        continue
                    if lock and lock["locked_date"] != casting_day.isoformat():
                        continue
                    candidates.append(state)
                candidates.sort(
                    key=lambda state: state_sort_key(
                        state,
                        bucket_ton,
                        day_total,
                    )
                )
                for state in candidates:
                    unit_ton = state["unit_ton"]
                    capacity_left = day_limit - day_total
                    units = choose_batch_units(state, capacity_left, config)
                    if units <= 0:
                        if unit_ton > float(config["casting_absolute_max_ton"]):
                            unplanned_reasons[state["order"]["id"]] = "单件重量超过浇筑绝对上限"
                        continue
                    reservation = reserve_molding(
                        conn,
                        state,
                        units,
                        casting_day,
                        base,
                        config,
                        molding_capacity,
                    )
                    if reservation is None:
                        unplanned_reasons[state["order"]["id"]] = "浇筑前造型产能不足"
                        continue
                    allocations, ready_units = reservation
                    batch_weight = round(units * unit_ton, 6)
                    batch_key = f"{state['order']['id']}-{casting_day.isoformat()}-{state['remaining_units']}"
                    for mold_day, mold_units in allocations:
                        mold_weight = round(mold_units * unit_ton, 6)
                        tasks.append(
                            task_row(
                                version=version,
                                day=mold_day,
                                order=state["order"],
                                process=state["molding_process"],
                                units=mold_units,
                                weight_ton=mold_weight,
                                workload_qty=mold_weight,
                                workload_unit="吨",
                                priority=state["priority"]["priority_level"],
                                related_casting=casting_day,
                                estimated_hours=None,
                                reason=f"为{casting_day.isoformat()}浇筑反推，必须当日完成",
                                locked=bool(state["lock"]),
                                batch_key=batch_key,
                                latest_finish=state["deadlines"].get(state["molding_process"]),
                            )
                        )
                    state["available_molded_units"] = max(
                        state["available_molded_units"] - ready_units,
                        0,
                    )
                    material = str(state["order"].get("material") or "").upper()
                    risk = None
                    reasons = []
                    if state["order"].get("is_urgent"):
                        reasons.append("急单")
                    if state["priority"]["is_overdue"]:
                        reasons.append("已逾期")
                    reasons.append(f"交期{state['order'].get('due_date') or '缺失'}")
                    reasons.append("造型已完成或可在浇筑前完成")
                    if material.startswith("QT"):
                        reasons.append("QT按炉次组合")
                        if batch_weight < float(config["qt_furnace_min_ton"]):
                            risk = "QT批次不足3吨，需人工确认"
                    reasons.append(f"预计当日浇筑负载{day_total + batch_weight:.2f}吨")
                    casting_task = task_row(
                        version=version,
                        day=casting_day,
                        order=state["order"],
                        process="浇筑",
                        units=units,
                        weight_ton=batch_weight,
                        workload_qty=batch_weight,
                        workload_unit="吨",
                        priority=state["priority"]["priority_level"],
                        related_casting=casting_day,
                        estimated_hours=None,
                        reason="；".join(reasons),
                        risk=risk,
                        locked=bool(state["lock"]),
                        batch_key=batch_key,
                        latest_finish=state["deadlines"].get("浇筑"),
                    )
                    tasks.append(casting_task)
                    casting_tasks.append(casting_task)
                    day_total += batch_weight
                    bucket_ton[state["bucket"]] += batch_weight
                    state["remaining_units"] -= units
                    unplanned_reasons.pop(state["order"]["id"], None)
                    made_progress = True
                    break

        for state in states:
            if state["remaining_units"] <= 0:
                continue
            reason = unplanned_reasons.get(
                state["order"]["id"],
                "30个生产日内浇筑能力不足",
            )
            add_issue(
                conn,
                version,
                state["order"],
                "CASTING_UNPLANNED",
                f"{reason}，剩余{state['remaining_units']}件未排入浇筑主计划",
                "阻断",
            )

        schedule_post_processes(
            conn,
            version,
            casting_tasks,
            order_map,
            config,
            tasks,
        )
        tasks.sort(
            key=lambda task: (
                task["date"],
                PROCESSES.index(task["process_name"])
                if task["process_name"] in PROCESSES
                else 99,
                PRIORITY_RANK.get(task["priority_level"], 9),
                task["order_id"],
                task["batch_key"] or "",
            )
        )
        insert_tasks(conn, tasks)
        adjustment_count = record_adjustments(conn, version, previous_version)
        issue_count = conn.execute(
            "SELECT COUNT(*) AS c FROM data_issues WHERE schedule_version=?",
            (version,),
        ).fetchone()["c"]
        max_date = max((task["date"] for task in tasks), default=base.isoformat())
        summary = {
            "task_count": len(tasks),
            "casting_task_count": len(casting_tasks),
            "unplanned_orders": sum(1 for state in states if state["remaining_units"] > 0),
            "issue_count": issue_count,
            "adjustment_count": adjustment_count,
            "from_date": base.isoformat(),
            "to_date": max_date,
            "horizon_production_days": int(config["horizon_days"]),
            "algorithm": "交期倒排各工序最晚节点→安排浇筑→反推造型→按产能校验后段",
        }
        conn.execute(
            """
            INSERT INTO schedule_runs
            (schedule_version, run_time, trigger_type, summary, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (version, now_iso(), trigger_type, json_dumps(summary), now_iso()),
        )
        refresh_order_status(conn)
        return {"schedule_version": version, "summary": summary}


def refresh_order_status(conn) -> None:
    for row in conn.execute("SELECT id FROM orders").fetchall():
        progress = conn.execute(
            """
            SELECT SUM(remaining_qty) AS remaining, SUM(completed_qty) AS completed
            FROM order_progress WHERE order_id=?
            """,
            (row["id"],),
        ).fetchone()
        remaining = float(progress["remaining"] or 0)
        completed = float(progress["completed"] or 0)
        status = "已完成" if remaining <= 0 else ("进行中" if completed > 0 else "未开始")
        current = conn.execute(
            "SELECT status FROM orders WHERE id=?",
            (row["id"],),
        ).fetchone()
        if current and current["status"] not in ("暂停", "延期"):
            conn.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE id=?",
                (status, now_iso(), row["id"]),
            )


def latest_version(conn) -> str | None:
    row = conn.execute(
        "SELECT schedule_version FROM schedule_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["schedule_version"] if row else None


def ensure_schedule(conn) -> str | None:
    version = latest_version(conn)
    if version:
        return version
    return generate_schedule("initial")["schedule_version"]


def task_summary(
    tasks: list[dict[str, Any]],
    caps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    totals = defaultdict(float)
    units = defaultdict(int)
    weight = defaultdict(float)
    for task in tasks:
        totals[task["process_name"]] += float(task["planned_qty"])
        units[task["process_name"]] += int(task.get("planned_units") or 0)
        weight[task["process_name"]] += float(task.get("planned_weight_ton") or 0)
    result = []
    for process in PROCESSES:
        capacity = float(caps.get(process, {}).get("capacity", 0))
        total = totals.get(process, 0.0)
        result.append(
            {
                "process_name": process,
                "planned_qty": round(total, 2),
                "planned_units": units.get(process, 0),
                "planned_weight_ton": round(weight.get(process, 0), 2),
                "capacity": capacity,
                "unit": caps.get(process, {}).get("unit", "件"),
                "usage_rate": round(total / capacity * 100, 1) if capacity else 0,
            }
        )
    return result


def risk_orders(conn, base: date) -> list[dict[str, Any]]:
    caps = default_capacities(conn)
    orders = rows_to_dicts(
        conn.execute(
            "SELECT * FROM orders WHERE status NOT IN ('已完成')"
        ).fetchall()
    )
    risks = []
    for order in orders:
        progress = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM order_progress
                WHERE order_id=? AND remaining_qty > 0
                """,
                (order["id"],),
            ).fetchall()
        )
        if not progress:
            continue
        priority = order_priority(order, progress, caps, base)
        if priority["priority_level"] in ("P0", "P1", "P2") or priority["is_overdue"]:
            risks.append({**order, **priority})
    risks.sort(
        key=lambda item: (
            PRIORITY_RANK.get(item["priority_level"], 9),
            item["due_date"] or "9999-12-31",
            item["id"],
        )
    )
    return risks


def dashboard_data(plan_week_start: date | None = None) -> dict[str, Any]:
    with get_conn() as conn:
        version = ensure_schedule(conn)
        today = date.today()
        config = scheduler_config(conn)
        future_days = production_days(today, int(config["horizon_days"]), config)
        tomorrow = future_days[1] if len(future_days) > 1 else today + timedelta(days=1)
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM schedule_tasks
                WHERE schedule_version=?
                ORDER BY date, priority_level, process_name, order_id
                """,
                (version,),
            ).fetchall()
        )
        decorate_task_identifiers(rows)
        deadline_alerts = build_deadline_alerts(rows, today)
        alert_by_task_id = {
            alert["task_id"]: alert
            for alert in deadline_alerts
            if alert.get("task_id") is not None
        }
        for row in rows:
            alert = alert_by_task_id.get(row.get("id"))
            row["deadline_alert_text"] = alert["message"] if alert else None
            row["deadline_alert_level"] = alert["level"] if alert else None
            row["days_to_deadline"] = (
                alert["days_to_deadline"] if alert else None
            )
            row["days_to_casting"] = (
                alert["days_to_casting"] if alert else None
            )
        tasks_today = [row for row in rows if row["date"] == today.isoformat()]
        tasks_tomorrow = [row for row in rows if row["date"] == tomorrow.isoformat()]
        today_casting = [row for row in tasks_today if row["process_name"] == "浇筑"]
        today_molding = [row for row in tasks_today if row["process_name"] in ("木模造型", "消失模造型")]
        today_opening = [row for row in tasks_today if row["process_name"] == "开箱"]
        today_later = [row for row in tasks_today if row["process_name"] in ("打磨", "加工", "检验")]
        tomorrow_casting = [row for row in tasks_tomorrow if row["process_name"] == "浇筑"]
        casting_load = []
        for day in future_days:
            day_tasks = [
                row for row in rows
                if row["date"] == day.isoformat() and row["process_name"] == "浇筑"
            ]
            ton = round(sum(float(row.get("planned_weight_ton") or 0) for row in day_tasks), 2)
            casting_load.append(
                {
                    "date": day.isoformat(),
                    "planned_weight_ton": ton,
                    "planned_units": sum(int(row.get("planned_units") or 0) for row in day_tasks),
                    "target_ton": float(config["casting_target_ton"]),
                    "normal_min_ton": float(config["casting_normal_min_ton"]),
                    "normal_max_ton": float(config["casting_normal_max_ton"]),
                    "usage_rate": round(ton / float(config["casting_target_ton"]) * 100, 1),
                    "status": (
                        "超上限" if ton > float(config["casting_absolute_max_ton"])
                        else "正常" if ton >= float(config["casting_normal_min_ton"])
                        else "低负载"
                    ),
                }
            )
        risks = risk_orders(conn, today)
        issues = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM data_issues
                WHERE schedule_version=?
                ORDER BY CASE severity WHEN '阻断' THEN 0 ELSE 1 END, id
                """,
                (version,),
            ).fetchall()
        )
        run = conn.execute(
            "SELECT * FROM schedule_runs WHERE schedule_version=?",
            (version,),
        ).fetchone()
        adjustments = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM schedule_adjustments
                WHERE schedule_version=? ORDER BY id DESC LIMIT 20
                """,
                (version,),
            ).fetchall()
        )
        active_scope_locks = rows_to_dicts(
            conn.execute(
                "SELECT * FROM plan_scope_locks ORDER BY scope_type, scope_value"
            ).fetchall()
        )
        actual_week_start, actual_week_end = week_bounds(today)
        current_week_start, current_week_end = week_bounds(
            plan_week_start or today
        )
        next_week_start = current_week_start + timedelta(days=7)
        next_week_end = next_week_start + timedelta(days=6)
        current_week_tasks = [
            row for row in rows
            if current_week_start.isoformat() <= row["date"] <= current_week_end.isoformat()
        ]
        current_week_tasks.sort(key=shop_floor_sort_key)
        week_plan_rows = build_week_plan_rows(current_week_tasks)
        next_week_tasks = [
            row for row in rows
            if next_week_start.isoformat() <= row["date"] <= next_week_end.isoformat()
        ]
        next_week_tasks.sort(key=shop_floor_sort_key)
        week_casting = [
            row for row in current_week_tasks if row["process_name"] == "浇筑"
        ]
        week_molding = [
            row for row in current_week_tasks
            if row["process_name"] in ("木模造型", "消失模造型")
        ]
        next_week_prep = [
            row for row in week_molding
            if row.get("related_casting_date")
            and next_week_start.isoformat()
            <= row["related_casting_date"]
            <= next_week_end.isoformat()
        ]
        week_opening = [
            row for row in current_week_tasks if row["process_name"] == "开箱"
        ]
        week_later = [
            row for row in current_week_tasks
            if row["process_name"] in ("打磨", "加工", "检验")
        ]
        weekly_casting_days = []
        for offset in range(7):
            day = current_week_start + timedelta(days=offset)
            day_tasks = [
                row for row in week_casting if row["date"] == day.isoformat()
            ]
            ton = round(
                sum(float(row.get("planned_weight_ton") or 0) for row in day_tasks),
                2,
            )
            production_day = day.weekday() in set(config["working_weekdays"])
            status = (
                "休息日"
                if not production_day
                else "超绝对上限"
                if ton > float(config["casting_absolute_max_ton"])
                else "超正常范围"
                if ton > float(config["casting_normal_max_ton"])
                else "低于推荐"
                if ton < float(config["casting_normal_min_ton"])
                else "正常"
            )
            weekly_casting_days.append(
                {
                    "date": day.isoformat(),
                    "weekday": "一二三四五六日"[day.weekday()],
                    "is_production_day": production_day,
                    "planned_units": sum(
                        int(row.get("planned_units") or 0) for row in day_tasks
                    ),
                    "planned_weight_ton": ton,
                    "usage_rate": round(
                        ton / float(config["casting_target_ton"]) * 100, 1
                    ),
                    "status": status,
                    "tasks": day_tasks,
                }
            )
        four_week_load = []
        for week_offset in range(4):
            start = current_week_start + timedelta(days=week_offset * 7)
            end = start + timedelta(days=6)
            week_rows = [
                row for row in rows
                if start.isoformat() <= row["date"] <= end.isoformat()
            ]
            casting_rows = [
                row for row in week_rows if row["process_name"] == "浇筑"
            ]
            production_day_count = sum(
                1
                for offset in range(7)
                if (start + timedelta(days=offset)).weekday()
                in set(config["working_weekdays"])
            )
            capacity_ton = (
                production_day_count * float(config["casting_target_ton"])
            )
            planned_ton = round(
                sum(
                    float(row.get("planned_weight_ton") or 0)
                    for row in casting_rows
                ),
                2,
            )
            four_week_load.append(
                {
                    "week_start": start.isoformat(),
                    "week_end": end.isoformat(),
                    "iso_week": iso_week_label(start),
                    "planned_units": sum(
                        int(row.get("planned_units") or 0)
                        for row in casting_rows
                    ),
                    "planned_weight_ton": planned_ton,
                    "target_capacity_ton": capacity_ton,
                    "usage_rate": round(
                        planned_ton / capacity_ton * 100, 1
                    ) if capacity_ton else 0,
                    "task_count": len(week_rows),
                }
            )
        delayed_orders = {
            row["order_id"]
            for row in rows
            if row.get("due_date") and row["date"] > row["due_date"]
        }
        capacity_risk_count = sum(
            1
            for item in weekly_casting_days
            if item["is_production_day"] and item["status"] != "正常"
        )
        weekly_summary = {
            "casting_units": sum(
                int(row.get("planned_units") or 0) for row in week_casting
            ),
            "casting_weight_ton": round(
                sum(float(row.get("planned_weight_ton") or 0) for row in week_casting),
                2,
            ),
            "molding_units": sum(
                int(row.get("planned_units") or 0) for row in week_molding
            ),
            "opening_units": sum(
                int(row.get("planned_units") or 0) for row in week_opening
            ),
            "later_units": sum(
                int(row.get("planned_units") or 0) for row in week_later
            ),
            "capacity_risk_count": capacity_risk_count,
            "data_issue_count": len(issues),
            "delayed_order_count": len(delayed_orders),
            "deadline_alert_count": len(deadline_alerts),
            "urgent_deadline_alert_count": sum(
                1
                for alert in deadline_alerts
                if alert["level"] in ("overdue", "urgent")
            ),
            "average_casting_usage_rate": round(
                sum(
                    item["usage_rate"]
                    for item in weekly_casting_days
                    if item["is_production_day"]
                )
                / max(
                    sum(
                        1
                        for item in weekly_casting_days
                        if item["is_production_day"]
                    ),
                    1,
                ),
                1,
            ),
            "plan_row_count": len(week_plan_rows),
        }
        week_task_ids = {
            row["id"] for row in current_week_tasks if row.get("id") is not None
        }
        week_deadline_alerts = [
            alert
            for alert in deadline_alerts
            if alert.get("task_id") in week_task_ids
        ]
        available_starts = {
            actual_week_start + timedelta(days=offset * 7)
            for offset in range(8)
        }
        available_starts.add(current_week_start)
        for row in rows:
            row_date = parse_date(row.get("date"))
            if row_date and row_date >= actual_week_start:
                available_starts.add(week_bounds(row_date)[0])
        available_weeks = []
        for start in sorted(available_starts):
            end = start + timedelta(days=6)
            week_rows = [
                row for row in rows
                if start.isoformat() <= row["date"] <= end.isoformat()
            ]
            casting_rows = [
                row for row in week_rows if row["process_name"] == "浇筑"
            ]
            plan_rows = build_week_plan_rows(week_rows)
            available_weeks.append(
                {
                    "week_start": start.isoformat(),
                    "week_end": end.isoformat(),
                    "iso_week": iso_week_label(start),
                    "relation": week_relation_label(start, actual_week_start),
                    "task_count": len(plan_rows),
                    "casting_units": sum(
                        int(row.get("planned_units") or 0)
                        for row in casting_rows
                    ),
                    "casting_weight_ton": round(
                        sum(
                            float(row.get("planned_weight_ton") or 0)
                            for row in casting_rows
                        ),
                        2,
                    ),
                }
            )
        return {
            "schedule_version": version,
            "today": today.isoformat(),
            "tomorrow": tomorrow.isoformat(),
            "last_run_time": run["run_time"] if run else None,
            "run_summary": json.loads(run["summary"]) if run and run["summary"] else {},
            "summary": task_summary(tasks_today, get_capacities(conn, today)),
            "today_tasks": tasks_today,
            "tomorrow_tasks": tasks_tomorrow,
            "today_casting": today_casting,
            "today_molding": today_molding,
            "today_opening": today_opening,
            "today_later": today_later,
            "tomorrow_casting": tomorrow_casting,
            "casting_load": casting_load,
            "risk_orders": risks[:20],
            "overdue_orders": [item for item in risks if item["is_overdue"]],
            "quantity_issues": [
                issue for issue in issues
                if issue["issue_code"] in ("PLAN_UNITS_MISSING", "UNIT_WEIGHT_MISSING")
            ],
            "issues": issues,
            "adjustments": adjustments,
            "active_scope_locks": active_scope_locks,
            "current_week_start": actual_week_start.isoformat(),
            "current_week_end": actual_week_end.isoformat(),
            "week_start": current_week_start.isoformat(),
            "week_end": current_week_end.isoformat(),
            "iso_week": iso_week_label(current_week_start),
            "week_relation": week_relation_label(
                current_week_start,
                actual_week_start,
            ),
            "next_week_start": next_week_start.isoformat(),
            "next_week_end": next_week_end.isoformat(),
            "next_week_relation": week_relation_label(
                next_week_start,
                actual_week_start,
            ),
            "available_weeks": available_weeks,
            "weekly_summary": weekly_summary,
            "deadline_alerts": deadline_alerts,
            "week_deadline_alerts": week_deadline_alerts,
            "weekly_casting_days": weekly_casting_days,
            "week_tasks": current_week_tasks,
            "week_plan_rows": week_plan_rows,
            "week_casting": week_casting,
            "week_molding": week_molding,
            "next_week_prep_molding": next_week_prep,
            "week_opening": week_opening,
            "week_grinding": [
                row for row in week_later if row["process_name"] == "打磨"
            ],
            "week_machining": [
                row for row in week_later if row["process_name"] == "加工"
            ],
            "week_inspection": [
                row for row in week_later if row["process_name"] == "检验"
            ],
            "next_week_tasks": next_week_tasks,
            "four_week_load": four_week_load,
        }


def daily_reminder_data() -> dict[str, Any]:
    data = dashboard_data()
    reminders = list(data["deadline_alerts"])
    today = data["today"]

    for task in data["week_casting"]:
        if task["date"] != today:
            continue
        units = task.get("planned_units")
        weight = task.get("planned_weight_ton")
        quantity_text = f"{int(units)}件" if units is not None else "该批次"
        if weight is not None:
            quantity_text += f"（{float(weight):.2f}吨）"
        reminders.append(
            {
                "task_id": task.get("id"),
                "batch_key": task.get("batch_key"),
                "order_id": task.get("order_id"),
                "order_no": task.get("order_no"),
                "product_name": (
                    task.get("product_name")
                    or task.get("product_type")
                    or "铸件"
                ),
                "process_name": "浇筑",
                "group_name": "浇筑组",
                "planned_units": units,
                "planned_weight_ton": weight,
                "deadline_date": today,
                "casting_date": today,
                "days_to_deadline": 0,
                "days_to_casting": 0,
                "level": "urgent",
                "message": (
                    f"浇筑组须在今天完成订单 {task.get('order_no') or '未编号订单'} "
                    f"的浇筑任务{quantity_text}，并及时反馈异常。"
                ),
            }
        )

    level_rank = {"overdue": 0, "urgent": 1, "warning": 2, "notice": 3}
    reminders.sort(
        key=lambda item: (
            level_rank.get(item["level"], 9),
            item["deadline_date"],
            str(item.get("order_no") or ""),
            item["process_name"],
        )
    )
    group_summary: dict[str, dict[str, Any]] = {}
    for reminder in reminders:
        group = reminder["group_name"]
        summary = group_summary.setdefault(
            group,
            {
                "group_name": group,
                "total": 0,
                "overdue": 0,
                "urgent": 0,
                "warning": 0,
                "notice": 0,
            },
        )
        summary["total"] += 1
        summary[reminder["level"]] += 1

    return {
        "schedule_version": data["schedule_version"],
        "today": today,
        "generated_at": now_iso(),
        "summary": {
            "total": len(reminders),
            "overdue": sum(
                1 for item in reminders if item["level"] == "overdue"
            ),
            "urgent": sum(
                1 for item in reminders if item["level"] == "urgent"
            ),
            "warning": sum(
                1 for item in reminders if item["level"] == "warning"
            ),
            "notice": sum(
                1 for item in reminders if item["level"] == "notice"
            ),
        },
        "group_summary": sorted(
            group_summary.values(),
            key=lambda item: (
                -(item["overdue"] + item["urgent"]),
                item["group_name"],
            ),
        ),
        "reminders": reminders,
        "rule_note": (
            "提醒每天按当前日期、对应浇筑日期和最晚完成日期自动重算；"
            "它提示班组必须守住的节点，不替代班组在周内自行安排具体作业顺序。"
        ),
    }


def apply_filters(
    rows: list[dict[str, Any]],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    def ok(row: dict[str, Any]) -> bool:
        if filters.get("q"):
            query = str(filters["q"]).lower()
            if query not in str(row.get("order_no", "")).lower() and query not in str(row.get("customer", "")).lower():
                return False
        for key, column in (
            ("product_type", "product_type"),
            ("priority", "priority_level"),
            ("process", "process_name"),
        ):
            if filters.get(key) and row.get(column) != filters[key]:
                return False
        if filters.get("urgent") and not row.get("is_urgent"):
            return False
        if filters.get("date_from") and row["date"] < filters["date_from"]:
            return False
        if filters.get("date_to") and row["date"] > filters["date_to"]:
            return False
        if filters.get("overdue"):
            due = parse_date(row.get("due_date"))
            if not due or due >= date.today():
                return False
        return True
    return [row for row in rows if ok(row)]


def full_schedule(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    with get_conn() as conn:
        version = ensure_schedule(conn)
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM schedule_tasks
                WHERE schedule_version=?
                ORDER BY date, process_name, priority_level, order_id
                """,
                (version,),
            ).fetchall()
        )
        decorate_task_identifiers(rows)
        rows = apply_filters(rows, filters)
        by_date_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_date_map[row["date"]].append(row)
        by_date = [
            {
                "date": day,
                "summary": task_summary(tasks, get_capacities(conn, parse_date(day) or date.today())),
                "tasks": tasks,
            }
            for day, tasks in sorted(by_date_map.items())
        ]
        by_order_map: dict[int, dict[str, Any]] = {}
        for task in rows:
            entry = by_order_map.setdefault(
                task["order_id"],
                {
                    "order_id": task["order_id"],
                    "order_no": task["order_no"],
                    "customer": task["customer"],
                    "product_type": task["product_type"],
                    "due_date": task["due_date"],
                    "priority_level": task["priority_level"],
                    "is_urgent": task["is_urgent"],
                    "tasks": [],
                },
            )
            entry["tasks"].append(task)
        by_order = []
        for entry in by_order_map.values():
            dates = [parse_date(task["date"]) for task in entry["tasks"]]
            valid_dates = [day for day in dates if day]
            complete = max(valid_dates).isoformat() if valid_dates else None
            due = parse_date(entry["due_date"])
            delay_days = max(((parse_date(complete) - due).days if complete and due else 0), 0)
            entry.update(
                estimated_finish_date=complete,
                on_time=delay_days == 0,
                delay_days=delay_days,
            )
            by_order.append(entry)
        by_order.sort(
            key=lambda item: (
                PRIORITY_RANK.get(item["priority_level"], 9),
                item["due_date"] or "9999-12-31",
                item["order_id"],
            )
        )
        by_process = []
        for process in PROCESSES:
            process_rows = [row for row in rows if row["process_name"] == process]
            day_rows = []
            for day in sorted({row["date"] for row in process_rows}):
                tasks = [row for row in process_rows if row["date"] == day]
                cap = get_capacities(conn, parse_date(day) or date.today()).get(
                    process,
                    {"capacity": 0, "unit": ""},
                )
                total = sum(float(task["planned_qty"]) for task in tasks)
                day_rows.append(
                    {
                        "date": day,
                        "planned_qty": round(total, 2),
                        "planned_units": sum(int(task.get("planned_units") or 0) for task in tasks),
                        "planned_weight_ton": round(sum(float(task.get("planned_weight_ton") or 0) for task in tasks), 2),
                        "capacity": cap["capacity"],
                        "unit": cap["unit"],
                        "usage_rate": round(total / cap["capacity"] * 100, 1) if cap["capacity"] else 0,
                        "overloaded": total > cap["capacity"] if cap["capacity"] else False,
                        "free_capacity": round(max(cap["capacity"] - total, 0), 2) if cap["capacity"] else 0,
                        "tasks": tasks,
                    }
                )
            by_process.append({"process_name": process, "days": day_rows})
        issues = rows_to_dicts(
            conn.execute(
                "SELECT * FROM data_issues WHERE schedule_version=? ORDER BY id",
                (version,),
            ).fetchall()
        )
        by_week_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            start = row.get("week_start") or week_bounds(
                parse_date(row["date"]) or date.today()
            )[0].isoformat()
            by_week_map[start].append(row)
        by_week = [
            {
                "week_start": start,
                "week_end": (
                    (parse_date(start) or date.today()) + timedelta(days=6)
                ).isoformat(),
                "iso_week": iso_week_label(
                    parse_date(start) or date.today()
                ),
                "tasks": tasks,
            }
            for start, tasks in sorted(by_week_map.items())
        ]
        return {
            "by_date": by_date,
            "by_week": by_week,
            "by_order": by_order,
            "by_process": by_process,
            "gantt": by_order,
            "issues": issues,
        }


def stats_data() -> dict[str, Any]:
    with get_conn() as conn:
        risks = risk_orders(conn, date.today())
        by_type = rows_to_dicts(
            conn.execute(
                """
                SELECT product_type, COUNT(*) AS order_count,
                       SUM(quantity) AS quantity, SUM(weight_ton) AS weight_ton
                FROM orders WHERE status != '已完成' GROUP BY product_type
                """
            ).fetchall()
        )
        by_process = rows_to_dicts(
            conn.execute(
                """
                SELECT process_name, unit, SUM(remaining_qty) AS remaining_qty
                FROM order_progress
                WHERE remaining_qty > 0 GROUP BY process_name, unit
                """
            ).fetchall()
        )
        risk_counts = defaultdict(int)
        for item in risks:
            risk_counts[item["priority_level"]] += 1
        total_orders = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status != '已完成'"
        ).fetchone()["c"]
        version = latest_version(conn)
        issue_count = conn.execute(
            "SELECT COUNT(*) AS c FROM data_issues WHERE schedule_version=?",
            (version,),
        ).fetchone()["c"] if version else 0
        return {
            "open_orders": total_orders,
            "by_product_type": by_type,
            "by_process": by_process,
            "risk_counts": dict(risk_counts),
            "issue_count": issue_count,
            "can_finish_on_time": not any(item["priority_level"] == "P0" for item in risks),
        }

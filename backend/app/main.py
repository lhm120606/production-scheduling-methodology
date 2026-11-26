from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .assistant import answer_question, get_ai_config, save_ai_config
from .db import APP_BASE_DIR, BASE_DIR, get_conn, init_db, now_iso, rows_to_dicts
from .excel_import import import_orders, preview_excel
from .exporter import export_full, export_simple
from .models import AiAskRequest, AiConfigPayload, CapacityPayload, CheckinRequest, ImportRequest, OrderPayload, OverridePayload, PlanLockPayload, PlanScopeLockPayload
from .scheduler import (
    daily_reminder_data,
    dashboard_data,
    full_schedule,
    generate_schedule,
    stats_data,
)


app = FastAPI(title="国刚铸模滚动排产小工具")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174", "http://127.0.0.1:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()
    from .excel_import import enrich_existing_orders
    enrich_existing_orders()
    refresh_all_order_progress()
    with get_conn() as conn:
        has_orders = conn.execute("SELECT id FROM orders LIMIT 1").fetchone()
    generate_schedule("startup_rules_sync" if has_orders else "startup")


def refresh_all_order_progress() -> None:
    from .excel_import import sync_progress

    with get_conn() as conn:
        orders = rows_to_dicts(conn.execute("SELECT * FROM orders").fetchall())
        for order in orders:
            sync_progress(conn, order["id"], order)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/import/preview")
async def import_preview(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    if not file.filename.endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="只支持 .xlsx/.xlsm 文件")
    tmp = BASE_DIR / "work" / f"tmp_{file.filename}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(await file.read())
    try:
        return preview_excel(tmp, file.filename)
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/import/commit")
def import_commit(payload: ImportRequest) -> dict[str, Any]:
    try:
        result = import_orders(
            payload.file_id,
            payload.mapping,
            replace_existing=payload.replace_existing,
        )
        schedule = generate_schedule(
            "import_replace" if payload.replace_existing else "import"
        )
        return {"import": result, "schedule": schedule}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    return dashboard_data()


@app.get("/api/schedule/week")
def week_plan(
    week_start: str | None = Query(
        default=None,
        description="要查看的自然周内任意日期，格式 YYYY-MM-DD",
    ),
) -> dict[str, Any]:
    if not week_start:
        return dashboard_data()
    try:
        selected = datetime.strptime(week_start, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="week_start 必须是 YYYY-MM-DD 格式",
        ) from exc
    return dashboard_data(selected)


@app.get("/api/schedule/next-week")
def next_week_plan() -> dict[str, Any]:
    data = dashboard_data()
    return {
        "schedule_version": data["schedule_version"],
        "week_start": data["next_week_start"],
        "week_end": data["next_week_end"],
        "tasks": data["next_week_tasks"],
        "prep_molding": data["next_week_prep_molding"],
    }


@app.get("/api/schedule/four-week-load")
def four_week_load() -> dict[str, Any]:
    data = dashboard_data()
    return {
        "schedule_version": data["schedule_version"],
        "weeks": data["four_week_load"],
    }


@app.get("/api/reminders/daily")
def daily_reminders() -> dict[str, Any]:
    return daily_reminder_data()


@app.get("/api/orders")
def orders(status: str | None = None, product_type: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if product_type:
        clauses.append("product_type=?")
        params.append(product_type)
    if q:
        clauses.append("(order_no LIKE ? OR customer LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    sql = "SELECT * FROM orders"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY due_date IS NULL, due_date ASC, id DESC"
    with get_conn() as conn:
        data = rows_to_dicts(conn.execute(sql, params).fetchall())
        for row in data:
            row["progress"] = rows_to_dicts(conn.execute("SELECT * FROM order_progress WHERE order_id=? ORDER BY id", (row["id"],)).fetchall())
        return data


@app.post("/api/orders")
def create_order(payload: OrderPayload) -> dict[str, Any]:
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM orders WHERE order_no=?", (payload.order_no,)).fetchone()
        if exists:
            raise HTTPException(status_code=400, detail="订单号已存在")
        cur = conn.execute(
            """
            INSERT INTO orders
            (order_no, model_no, customer, product_name, material, product_type,
             molding_method, quantity, unit_weight_kg, weight_ton,
             due_date, status, is_urgent, remark, source_file, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '手动录入', ?, ?)
            """,
            (
                payload.order_no, payload.model_no, payload.customer, payload.product_name, payload.material,
                payload.product_type, payload.molding_method or payload.product_type, payload.quantity,
                payload.weight_ton * 1000 / payload.quantity if payload.quantity and payload.weight_ton else None,
                payload.weight_ton,
                payload.due_date, payload.status, int(payload.is_urgent), payload.remark, now_iso(), now_iso(),
            ),
        )
        from .excel_import import sync_progress
        sync_progress(conn, cur.lastrowid, payload.model_dump())
    return generate_schedule("new_order")


@app.post("/api/orders/analyze")
def analyze_order(payload: OrderPayload) -> dict[str, Any]:
    from .excel_import import estimate_opening_hours

    with get_conn() as conn:
        capacities = rows_to_dicts(conn.execute("SELECT process_name, daily_capacity, unit FROM capacity_config").fetchall())
    cap_map = {row["process_name"]: float(row["daily_capacity"] or 0) for row in capacities}
    workloads = [
        {"process_name": "木模造型" if payload.product_type != "泡沫实型" else "消失模造型", "qty": payload.weight_ton, "unit": "吨", "capacity": cap_map.get("消失模造型" if payload.product_type == "泡沫实型" else "木模造型", 0)},
        {"process_name": "浇筑", "qty": payload.weight_ton, "unit": "吨", "capacity": cap_map.get("浇筑", 0)},
        {"process_name": "开箱", "qty": estimate_opening_hours(payload.weight_ton, payload.quantity), "unit": "小时", "capacity": cap_map.get("开箱", 0)},
        {"process_name": "打磨", "qty": payload.quantity, "unit": "件", "capacity": cap_map.get("打磨", 0)},
        {"process_name": "加工", "qty": payload.quantity, "unit": "件", "capacity": cap_map.get("加工", 0)},
        {"process_name": "检验", "qty": payload.quantity, "unit": "件", "capacity": cap_map.get("检验", 0)},
    ]
    estimated_days = sum((item["qty"] / item["capacity"]) for item in workloads if item["capacity"] > 0)
    from datetime import date, datetime
    due_days = None
    if payload.due_date:
        due_days = (datetime.strptime(payload.due_date, "%Y-%m-%d").date() - date.today()).days
    risk = "数据不足，无法判断"
    if due_days is not None:
        risk = "建议谨慎接单，可能影响交期" if due_days - estimated_days < 3 else "当前产能下初步可接"
    return {
        "estimated_work_days": round(estimated_days, 2),
        "days_to_due": due_days,
        "buffer_days": round(due_days - estimated_days, 2) if due_days is not None else None,
        "risk": risk,
        "workloads": workloads,
        "note": "这是基于当前默认产能的接单前粗估，保存订单后系统会重新滚动排产并给出完整影响。",
    }


@app.put("/api/orders/{order_id}")
def update_order(order_id: int, payload: OrderPayload) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE orders SET order_no=?, model_no=?, customer=?, product_name=?, material=?,
                product_type=?, molding_method=?, quantity=?, unit_weight_kg=?, weight_ton=?,
                due_date=?, status=?, is_urgent=?, remark=?, updated_at=?
            WHERE id=?
            """,
            (
                payload.order_no, payload.model_no, payload.customer, payload.product_name, payload.material,
                payload.product_type, payload.molding_method or payload.product_type, payload.quantity,
                payload.weight_ton * 1000 / payload.quantity if payload.quantity and payload.weight_ton else None,
                payload.weight_ton,
                payload.due_date, payload.status, int(payload.is_urgent), payload.remark, now_iso(), order_id,
            ),
        )
        from .excel_import import sync_progress
        sync_progress(conn, order_id, payload.model_dump())
    return generate_schedule("order_update")


@app.post("/api/orders/{order_id}/status")
def update_order_status(order_id: int, status: str = Query(...)) -> dict[str, Any]:
    if status not in {"未开始", "进行中", "已完成", "暂停", "延期"}:
        raise HTTPException(status_code=400, detail="状态不合法")
    with get_conn() as conn:
        conn.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, now_iso(), order_id))
    return generate_schedule("order_status")


@app.get("/api/capacity")
def capacity() -> dict[str, Any]:
    with get_conn() as conn:
        return {
            "defaults": rows_to_dicts(conn.execute("SELECT * FROM capacity_config ORDER BY id").fetchall()),
            "overrides": rows_to_dicts(conn.execute("SELECT * FROM capacity_overrides ORDER BY date DESC, process_name").fetchall()),
            "history": rows_to_dicts(conn.execute("SELECT * FROM capacity_history ORDER BY id DESC LIMIT 100").fetchall()),
        }


@app.put("/api/capacity/default")
def set_capacity(payload: CapacityPayload) -> dict[str, Any]:
    with get_conn() as conn:
        old = conn.execute("SELECT daily_capacity FROM capacity_config WHERE process_name=?", (payload.process_name,)).fetchone()
        conn.execute(
            """
            INSERT INTO capacity_config (process_name, daily_capacity, unit, effective_from, remark)
            VALUES (?, ?, ?, date('now'), ?)
            ON CONFLICT(process_name) DO UPDATE SET daily_capacity=excluded.daily_capacity, unit=excluded.unit, remark=excluded.remark
            """,
            (payload.process_name, payload.daily_capacity, payload.unit, payload.remark),
        )
        conn.execute(
            "INSERT INTO capacity_history (process_name, old_capacity, new_capacity, unit, change_type, reason, created_at) VALUES (?, ?, ?, ?, '默认产能', ?, ?)",
            (payload.process_name, old["daily_capacity"] if old else None, payload.daily_capacity, payload.unit, payload.remark, now_iso()),
        )
    return generate_schedule("capacity_default")


@app.post("/api/capacity/override")
def set_override(payload: OverridePayload) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO capacity_overrides (date, process_name, capacity, unit, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, process_name) DO UPDATE SET capacity=excluded.capacity, unit=excluded.unit, reason=excluded.reason
            """,
            (payload.date, payload.process_name, payload.capacity, payload.unit, payload.reason, now_iso()),
        )
        conn.execute(
            "INSERT INTO capacity_history (process_name, old_capacity, new_capacity, unit, change_type, reason, created_at) VALUES (?, NULL, ?, ?, '临时产能', ?, ?)",
            (payload.process_name, payload.capacity, payload.unit, f"{payload.date} {payload.reason or ''}", now_iso()),
        )
    return generate_schedule("capacity_override")


@app.get("/api/schedule/full")
def schedule_full(
    q: str | None = None,
    product_type: str | None = None,
    priority: str | None = None,
    process: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    overdue: bool = False,
    urgent: bool = False,
) -> dict[str, Any]:
    return full_schedule({"q": q, "product_type": product_type, "priority": priority, "process": process, "date_from": date_from, "date_to": date_to, "overdue": overdue, "urgent": urgent})


@app.post("/api/schedule/run")
def rerun() -> dict[str, Any]:
    return generate_schedule("manual")


@app.post("/api/schedule/lock")
def lock_plan(payload: PlanLockPayload) -> dict[str, Any]:
    with get_conn() as conn:
        if not conn.execute("SELECT id FROM orders WHERE id=?", (payload.order_id,)).fetchone():
            raise HTTPException(status_code=404, detail="订单不存在")
        conn.execute(
            """
            INSERT INTO plan_locks
            (order_id, process_name, locked_date, planned_units, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id, process_name) DO UPDATE SET
                locked_date=excluded.locked_date,
                planned_units=excluded.planned_units,
                reason=excluded.reason,
                created_at=excluded.created_at
            """,
            (payload.order_id, payload.process_name, payload.locked_date, payload.planned_units, payload.reason, now_iso()),
        )
    return generate_schedule("plan_lock")


@app.delete("/api/schedule/lock/{order_id}")
def unlock_plan(order_id: int, process_name: str = "浇筑") -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM plan_locks WHERE order_id=? AND process_name=?",
            (order_id, process_name),
        )
    return generate_schedule("plan_unlock")


@app.post("/api/schedule/scope-lock")
def lock_plan_scope(payload: PlanScopeLockPayload) -> dict[str, Any]:
    if payload.scope_type not in {"week", "day", "order", "batch"}:
        raise HTTPException(status_code=400, detail="锁定范围必须是 week、day、order 或 batch")
    with get_conn() as conn:
        version = conn.execute(
            "SELECT schedule_version FROM schedule_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not version:
            raise HTTPException(status_code=400, detail="当前没有可锁定的排产版本")
        conn.execute(
            """
            INSERT INTO plan_scope_locks
            (scope_type, scope_value, process_name, source_schedule_version, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_type, scope_value, process_name) DO UPDATE SET
                source_schedule_version=excluded.source_schedule_version,
                reason=excluded.reason,
                created_at=excluded.created_at
            """,
            (
                payload.scope_type,
                payload.scope_value,
                payload.process_name,
                version["schedule_version"],
                payload.reason,
                now_iso(),
            ),
        )
    return generate_schedule(f"{payload.scope_type}_lock")


@app.delete("/api/schedule/scope-lock")
def unlock_plan_scope(
    scope_type: str,
    scope_value: str,
    process_name: str = "浇筑",
) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM plan_scope_locks WHERE scope_type=? AND scope_value=? AND process_name=?",
            (scope_type, scope_value, process_name),
        )
    return generate_schedule(f"{scope_type}_unlock")


@app.get("/api/issues")
def issues() -> list[dict[str, Any]]:
    with get_conn() as conn:
        version = conn.execute(
            "SELECT schedule_version FROM schedule_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not version:
            return []
        return rows_to_dicts(
            conn.execute(
                "SELECT * FROM data_issues WHERE schedule_version=? ORDER BY id",
                (version["schedule_version"],),
            ).fetchall()
        )


@app.get("/api/schedule/adjustments")
def adjustments() -> list[dict[str, Any]]:
    with get_conn() as conn:
        version = conn.execute(
            "SELECT schedule_version FROM schedule_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not version:
            return []
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM schedule_adjustments
                WHERE schedule_version=? ORDER BY id DESC
                """,
                (version["schedule_version"],),
            ).fetchall()
        )


@app.get("/api/schedule/history")
def history() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM schedule_runs ORDER BY id DESC LIMIT 100").fetchall())


@app.post("/api/checkins")
def checkin(payload: CheckinRequest) -> dict[str, Any]:
    if not payload.items:
        raise HTTPException(status_code=400, detail="没有打卡任务")
    with get_conn() as conn:
        accepted = duplicates = 0
        for index, item in enumerate(payload.items):
            business_key = (
                f"{payload.idempotency_key}:{index}"
                if payload.idempotency_key
                else f"{payload.date}:{item.task_id or 0}:{item.order_id}:{item.process_name}"
            )
            cursor = conn.execute(
                """
                INSERT INTO daily_checkins
                (business_key, date, task_id, order_id, process_name, planned_qty, actual_qty,
                 planned_units, actual_units, status, reason, remark, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(business_key) DO NOTHING
                """,
                (
                    business_key,
                    payload.date,
                    item.task_id,
                    item.order_id,
                    item.process_name,
                    item.planned_qty,
                    item.actual_qty,
                    item.planned_units,
                    item.actual_units,
                    item.status,
                    item.reason,
                    item.remark,
                    now_iso(),
                ),
            )
            if cursor.rowcount == 0:
                duplicates += 1
                continue
            accepted += 1
            progress = conn.execute("SELECT * FROM order_progress WHERE order_id=? AND process_name=?", (item.order_id, item.process_name)).fetchone()
            if progress:
                completed = min(float(progress["planned_qty"]), float(progress["completed_qty"]) + item.actual_qty)
                remaining = max(float(progress["planned_qty"]) - completed, 0)
                status = "已完成" if remaining <= 0 else ("进行中" if completed > 0 else "未开始")
                conn.execute(
                    "UPDATE order_progress SET completed_qty=?, remaining_qty=?, status=?, updated_at=? WHERE id=?",
                    (completed, remaining, status, now_iso(), progress["id"]),
                )
    result = generate_schedule("checkin")
    return {**result, "checkin": {"accepted": accepted, "duplicates": duplicates}}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    return stats_data()


@app.get("/api/export/simple")
def export_simple_endpoint(
    week_start: str | None = Query(default=None),
) -> FileResponse:
    selected = None
    if week_start:
        try:
            selected = datetime.strptime(week_start, "%Y-%m-%d").date()
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="week_start 必须是 YYYY-MM-DD 格式",
            ) from exc
    path = export_simple(selected)
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/export/full")
def export_full_endpoint(
    week_start: str | None = Query(default=None),
) -> FileResponse:
    selected = None
    if week_start:
        try:
            selected = datetime.strptime(week_start, "%Y-%m-%d").date()
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="week_start 必须是 YYYY-MM-DD 格式",
            ) from exc
    path = export_full(selected)
    return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/ai/config")
def ai_config() -> dict[str, Any]:
    return get_ai_config()


@app.put("/api/ai/config")
def update_ai_config(payload: AiConfigPayload) -> dict[str, Any]:
    return save_ai_config(payload.model_dump())


@app.post("/api/ai/ask")
def ai_ask(payload: AiAskRequest) -> dict[str, Any]:
    return answer_question(payload.question)


FRONTEND_DIST = APP_BASE_DIR / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

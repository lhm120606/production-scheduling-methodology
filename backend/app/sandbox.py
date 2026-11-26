from __future__ import annotations

import re
import shutil
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from openpyxl import load_workbook

from .db import UPLOAD_DIR, get_conn, now_iso, rows_to_dicts


DIMENSION_PATTERNS = [
    re.compile(r"(?P<l>\d{2,5}(?:\.\d+)?)\s*[xX×*]\s*(?P<w>\d{2,5}(?:\.\d+)?)\s*[xX×*]\s*(?P<h>\d{2,5}(?:\.\d+)?)"),
    re.compile(r"长\s*(?P<l>\d{2,5}(?:\.\d+)?)\D{0,8}宽\s*(?P<w>\d{2,5}(?:\.\d+)?)\D{0,8}高\s*(?P<h>\d{2,5}(?:\.\d+)?)"),
    re.compile(r"L\s*(?P<l>\d{2,5}(?:\.\d+)?)\D{0,8}W\s*(?P<w>\d{2,5}(?:\.\d+)?)\D{0,8}H\s*(?P<h>\d{2,5}(?:\.\d+)?)", re.I),
]


def extract_dimensions_from_text(text: str) -> dict[str, float] | None:
    normalized = text.replace(",", " ").replace("，", " ").replace("毫米", "mm")
    for pattern in DIMENSION_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return {
                "length_mm": float(match.group("l")),
                "width_mm": float(match.group("w")),
                "height_mm": float(match.group("h")),
            }
    return None


def parse_dimension_text(value: Any) -> dict[str, Any] | None:
    text = str(value or "").strip().replace("×", "*").replace("X", "*").replace("x", "*")
    text = re.sub(r"\*{2,}", "*", text)
    rect = re.search(r"(?P<l>\d+(?:\.\d+)?)\s*\*\s*(?P<w>\d+(?:\.\d+)?)\s*\*\s*(?P<h>\d+(?:\.\d+)?)", text)
    if rect:
        return {
            "length_mm": float(rect.group("l")),
            "width_mm": float(rect.group("w")),
            "height_mm": float(rect.group("h")),
            "shape": "矩形",
            "clean_text": f"{rect.group('l')}*{rect.group('w')}*{rect.group('h')}",
        }
    round_box = re.search(r"(?P<d>\d+(?:\.\d+)?)\s*直径\s*\*\s*(?P<h>\d+(?:\.\d+)?)", text)
    if round_box:
        diameter = float(round_box.group("d"))
        return {
            "length_mm": diameter,
            "width_mm": diameter,
            "height_mm": float(round_box.group("h")),
            "shape": "圆形",
            "clean_text": f"{round_box.group('d')}直径*{round_box.group('h')}",
        }
    return None


def readable_text_from_file(path: Path, original_name: str) -> str:
    pieces = [original_name]
    raw = path.read_bytes()
    for encoding in ("utf-8", "gb18030", "latin1"):
        try:
            pieces.append(raw.decode(encoding, errors="ignore"))
            break
        except Exception:
            continue
    return "\n".join(pieces)


def match_sandbox(length_mm: float | None, width_mm: float | None, height_mm: float | None) -> dict[str, Any]:
    if not length_mm or not width_mm or not height_mm:
        return {"matched": None, "message": "当前数据不足，无法匹配沙箱：没有识别到完整长宽高。"}
    part_lw = sorted([float(length_mm), float(width_mm)], reverse=True)
    with get_conn() as conn:
        sandboxes = rows_to_dicts(conn.execute("SELECT * FROM sandboxes ORDER BY inner_length_mm, inner_width_mm, inner_height_mm").fetchall())
    if not sandboxes:
        return {"matched": None, "message": "当前还没有录入沙箱规格，无法判断能放哪个沙箱。"}
    candidates = []
    for sandbox in sandboxes:
        box_lw = sorted([float(sandbox["inner_length_mm"]), float(sandbox["inner_width_mm"])], reverse=True)
        if part_lw[0] <= box_lw[0] and part_lw[1] <= box_lw[1] and float(height_mm) <= float(sandbox["inner_height_mm"]):
            slack = (box_lw[0] * box_lw[1] * float(sandbox["inner_height_mm"])) - (part_lw[0] * part_lw[1] * float(height_mm))
            candidates.append((slack, sandbox))
    if not candidates:
        return {"matched": None, "message": "没有找到尺寸足够的沙箱，需要换大沙箱、拆分或外协确认。"}
    matched = sorted(candidates, key=lambda item: item[0])[0][1]
    return {"matched": matched, "message": f"建议使用 {matched['name']}，尺寸余量最接近，避免占用过大沙箱。"}


def import_sandbox_inventory(path: Path, original_name: str) -> dict[str, Any]:
    wb = load_workbook(path, data_only=True, read_only=False)
    ws = wb[wb.sheetnames[0]]
    parsed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for source_location, start_col in (("本厂", 1), ("于学政处", 6)):
        for row_no in range(2, ws.max_row + 1):
            row_values = [ws.cell(row_no, start_col + offset).value for offset in range(5)]
            seq, dim_text, qty_value, belt, remark = row_values
            if all(value in (None, "") for value in row_values):
                continue
            if str(seq or "").strip() == "总计":
                continue
            parsed_dim = parse_dimension_text(dim_text)
            if not parsed_dim:
                skipped.append({"row": row_no, "source_location": source_location, "reason": "尺寸格式无法识别", "values": row_values})
                continue
            try:
                qty = int(float(qty_value or 0))
            except (TypeError, ValueError):
                skipped.append({"row": row_no, "source_location": source_location, "reason": "件数无法识别", "values": row_values})
                continue
            if qty <= 0:
                skipped.append({"row": row_no, "source_location": source_location, "reason": "件数为0", "values": row_values})
                continue
            has_belt = 1 if str(belt or "").strip() == "有" else 0
            for index in range(1, qty + 1):
                parsed.append({
                    "name": f"{source_location}-{parsed_dim['clean_text']}-{index:03d}",
                    "sandbox_type": "普通",
                    "source_location": source_location,
                    "source_file": original_name,
                    "dimension_text": parsed_dim["clean_text"],
                    "length": parsed_dim["length_mm"],
                    "width": parsed_dim["width_mm"],
                    "height": parsed_dim["height_mm"],
                    "has_belt": has_belt,
                    "status": "可用",
                    "remark": "; ".join(part for part in [parsed_dim["shape"], str(remark or "").strip()] if part),
                })
    with get_conn() as conn:
        conn.execute("DELETE FROM sandboxes WHERE source_file = ?", (original_name,))
        for item in parsed:
            conn.execute(
                """
                INSERT INTO sandboxes
                (name, sandbox_type, source_location, source_file, dimension_text, inner_length_mm, inner_width_mm,
                 inner_height_mm, quantity, has_belt, status, available_from, remark, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, ?)
                """,
                (
                    item["name"],
                    item["sandbox_type"],
                    item["source_location"],
                    item["source_file"],
                    item["dimension_text"],
                    item["length"],
                    item["width"],
                    item["height"],
                    item["has_belt"],
                    item["status"],
                    item["remark"],
                    now_iso(),
                ),
            )
    return {
        "inserted": len(parsed),
        "skipped": len(skipped),
        "skipped_rows": skipped[:20],
        "source_file": original_name,
    }


def sandbox_stats() -> dict[str, Any]:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM sandboxes").fetchone()["c"]
        by_status = rows_to_dicts(conn.execute("SELECT status, COUNT(*) AS count FROM sandboxes GROUP BY status ORDER BY status").fetchall())
        by_source = rows_to_dicts(conn.execute("SELECT COALESCE(source_location, '手动') AS source_location, COUNT(*) AS count FROM sandboxes GROUP BY source_location ORDER BY source_location").fetchall())
        by_belt = rows_to_dicts(conn.execute("SELECT has_belt, COUNT(*) AS count FROM sandboxes GROUP BY has_belt ORDER BY has_belt DESC").fetchall())
        usage = rows_to_dicts(
            conn.execute(
                """
                SELECT date, status, COUNT(*) AS count
                FROM sandbox_usage
                WHERE schedule_version = (SELECT schedule_version FROM schedule_runs ORDER BY id DESC LIMIT 1)
                GROUP BY date, status
                ORDER BY date, status
                LIMIT 60
                """
            ).fetchall()
        )
    return {"total": total, "by_status": by_status, "by_source": by_source, "by_belt": by_belt, "usage": usage}


def candidate_sandboxes(conn, order: dict[str, Any]) -> list[dict[str, Any]]:
    length = order.get("length_mm")
    width = order.get("width_mm")
    height = order.get("height_mm")
    if not length or not width or not height:
        return []
    part_lw = sorted([float(length), float(width)], reverse=True)
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT * FROM sandboxes
            WHERE status = '可用'
            ORDER BY inner_length_mm, inner_width_mm, inner_height_mm, id
            """
        ).fetchall()
    )
    candidates = []
    for row in rows:
        box_lw = sorted([float(row["inner_length_mm"]), float(row["inner_width_mm"])], reverse=True)
        if part_lw[0] <= box_lw[0] and part_lw[1] <= box_lw[1] and float(height) <= float(row["inner_height_mm"]):
            slack = (box_lw[0] * box_lw[1] * float(row["inner_height_mm"])) - (part_lw[0] * part_lw[1] * float(height))
            candidates.append({**row, "slack": slack})
    return sorted(candidates, key=lambda item: (item["slack"], item["id"]))


def assign_schedule_sandboxes(conn, schedule_version: str) -> dict[str, int]:
    conn.execute("DELETE FROM sandbox_usage WHERE schedule_version = ?", (schedule_version,))
    conn.execute(
        """
        UPDATE schedule_tasks
        SET sandbox_id = NULL, sandbox_name = NULL, sandbox_status = NULL, sandbox_note = NULL
        WHERE schedule_version = ?
        """,
        (schedule_version,),
    )
    task_rows = rows_to_dicts(
        conn.execute(
            """
            SELECT t.*, o.length_mm, o.width_mm, o.height_mm, o.sandbox_id AS preferred_sandbox_id, o.sandbox_plan
            FROM schedule_tasks t
            JOIN orders o ON o.id = t.order_id
            WHERE t.schedule_version = ? AND t.process_name IN ('木模造型', '消失模造型', '浇筑', '开箱')
            ORDER BY t.date, t.priority_level, t.due_date, t.order_id
            """,
            (schedule_version,),
        ).fetchall()
    )
    by_order: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task in task_rows:
        by_order[int(task["order_id"])].append(task)

    reserved: dict[str, set[int]] = defaultdict(set)
    assigned_orders = missing_size = no_sandbox = conflict_days = 0
    for order_id, tasks in by_order.items():
        order = tasks[0]
        task_dates = sorted({task["date"] for task in tasks})
        molding_dates = [
            task["date"] for task in tasks
            if task["process_name"] in ("木模造型", "消失模造型")
        ]
        opening_dates = [
            task["date"] for task in tasks if task["process_name"] == "开箱"
        ]
        start_text = min(molding_dates or task_dates)
        end_text = max(opening_dates or task_dates)
        current = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
        dates = []
        while current <= end:
            dates.append(current.isoformat())
            current += timedelta(days=1)
        if not order.get("length_mm") or not order.get("width_mm") or not order.get("height_mm"):
            status = "缺少尺寸"
            reason = "订单没有长宽高，暂时不能自动匹配砂箱。"
            missing_size += 1
            sandbox = None
        else:
            preferred = None
            if order.get("preferred_sandbox_id"):
                preferred_row = conn.execute("SELECT * FROM sandboxes WHERE id = ?", (order["preferred_sandbox_id"],)).fetchone()
                preferred = dict(preferred_row) if preferred_row else None
            candidates = [preferred] if preferred else candidate_sandboxes(conn, order)
            candidates = [item for item in candidates if item]
            sandbox = None
            for candidate in candidates:
                if all(int(candidate["id"]) not in reserved[day] for day in dates):
                    sandbox = candidate
                    break
            if sandbox:
                status = "已安排"
                reason = "按订单尺寸匹配砂箱，占用到开箱任务完成。"
                assigned_orders += 1
                for day in dates:
                    reserved[day].add(int(sandbox["id"]))
            else:
                status = "砂箱不足"
                reason = "没有找到尺寸合适且日期不冲突的可用砂箱。"
                no_sandbox += 1

        for task in tasks:
            if status == "砂箱不足":
                conflict_days += 1
            conn.execute(
                """
                INSERT INTO sandbox_usage
                (schedule_version, date, order_id, order_no, process_name, sandbox_id, sandbox_name, status, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_version,
                    task["date"],
                    task["order_id"],
                    task["order_no"],
                    task["process_name"],
                    sandbox["id"] if sandbox else None,
                    sandbox["name"] if sandbox else None,
                    status,
                    reason,
                    now_iso(),
                ),
            )
            conn.execute(
                """
                UPDATE schedule_tasks
                SET sandbox_id = ?, sandbox_name = ?, sandbox_status = ?, sandbox_note = ?
                WHERE id = ?
                """,
                (sandbox["id"] if sandbox else None, sandbox["name"] if sandbox else None, status, reason, task["id"]),
            )
    return {
        "assigned_orders": assigned_orders,
        "missing_size_orders": missing_size,
        "no_sandbox_orders": no_sandbox,
        "conflict_tasks": conflict_days,
    }


def analyze_drawing(path: Path, original_name: str, order_no: str | None = None) -> dict[str, Any]:
    stored_name = f"drawing_{uuid4().hex}_{original_name}"
    stored_path = UPLOAD_DIR / stored_name
    shutil.copy2(path, stored_path)
    text = readable_text_from_file(stored_path, original_name)
    dimensions = extract_dimensions_from_text(text) or {"length_mm": None, "width_mm": None, "height_mm": None}
    match = match_sandbox(dimensions["length_mm"], dimensions["width_mm"], dimensions["height_mm"])
    matched = match["matched"]
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO drawing_uploads
            (order_no, original_name, stored_path, extracted_text, length_mm, width_mm, height_mm, matched_sandbox_id, fit_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_no,
                original_name,
                str(stored_path),
                text[:4000],
                dimensions["length_mm"],
                dimensions["width_mm"],
                dimensions["height_mm"],
                matched["id"] if matched else None,
                match["message"],
                now_iso(),
            ),
        )
    return {
        "drawing_id": cur.lastrowid,
        **dimensions,
        "sandbox": matched,
        "message": match["message"],
    }

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from openpyxl import load_workbook

from .db import UPLOAD_DIR, get_conn, now_iso, rows_to_dicts
from .models import FieldMapping


SYSTEM_FIELDS: dict[str, list[str]] = {
    "order_no": ["订单号", "合同号", "合同号/订单号", "合同", "产品代码", "铸件型号", "order"],
    "model_no": ["铸件型号", "型号", "产品代码", "图号"],
    "customer": ["客户", "客户名称", "客户名"],
    "product_name": ["铸件名称", "产品名称", "名称"],
    "material": ["材质", "材料", "牌号"],
    "product_type": ["产品类型", "工艺类型", "造型方式", "工艺类型\n（下拉）"],
    "molding_method": ["工艺类型", "木模/消失模", "造型方式"],
    "quantity": ["件数", "数量", "订单数量"],
    "weight_kg": ["单重", "单重（KG）", "重量kg", "重量（kg）"],
    "weight_ton": ["重量", "吨", "总重", "总重量", "weight_ton"],
    "due_date": ["交货日期", "交期", "ddl", "到货"],
    "remark": ["备注", "说明"],
    "is_urgent": ["急单", "是否急单", "紧急"],
}


def normalize_header(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def detect_header_row(rows: list[list[Any]]) -> int:
    best_idx = 0
    best_score = -1
    keywords = [kw for kws in SYSTEM_FIELDS.values() for kw in kws]
    normalized_keywords = [normalize_header(kw) for kw in keywords]
    for idx, row in enumerate(rows[:15]):
        score = 0
        for cell in row:
            text = normalize_header(cell)
            if not text:
                continue
            if any(kw in text or text in kw for kw in normalized_keywords):
                score += 1
        if score > best_score:
            best_score = score
            best_idx = idx
    return best_idx + 1


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    normalized = {header: normalize_header(header) for header in headers}
    for field, aliases in SYSTEM_FIELDS.items():
        normalized_aliases = [normalize_header(alias) for alias in aliases]
        for alias_text in normalized_aliases:
            exact = next(
                (header for header, text in normalized.items() if text == alias_text),
                None,
            )
            if exact:
                mapping[field] = exact
                break
        if field in mapping:
            continue
        candidates: list[tuple[int, str]] = []
        for header, text in normalized.items():
            if not text:
                continue
            for alias_text in normalized_aliases:
                if alias_text and (alias_text in text or text in alias_text):
                    candidates.append((abs(len(text) - len(alias_text)), header))
        if candidates:
            mapping[field] = min(candidates, key=lambda item: item[0])[1]
    return mapping


def preview_excel(path: Path, original_name: str) -> dict[str, Any]:
    stored_name = f"{uuid4().hex}_{original_name}"
    stored_path = UPLOAD_DIR / stored_name
    shutil.copy2(path, stored_path)

    wb = load_workbook(stored_path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    sample_rows = [list(row) for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 20), values_only=True)]
    header_row = detect_header_row(sample_rows)
    raw_headers = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True))
    headers = [str(value).strip() if value is not None else f"空列{idx + 1}" for idx, value in enumerate(raw_headers)]
    preview_rows = []
    for row in ws.iter_rows(min_row=header_row + 1, max_row=min(ws.max_row, header_row + 8), values_only=True):
        preview_rows.append({headers[idx]: format_cell(value) for idx, value in enumerate(row)})

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO upload_files (original_name, stored_path, sheet_name, header_row, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (original_name, str(stored_path), ws.title, header_row, now_iso()),
        )
        file_id = cur.lastrowid

    return {
        "file_id": file_id,
        "sheet_name": ws.title,
        "header_row": header_row,
        "headers": headers,
        "suggested_mapping": suggest_mapping(headers),
        "preview_rows": preview_rows,
    }


def format_cell(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value


def parse_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else 0.0


def parse_quantity(value: Any) -> int:
    if value in (None, ""):
        return 0
    text = str(value).strip()
    # Values such as 1/3, 2/3 and 3/3 identify an individual piece in a
    # multi-row batch. Each row is one piece; treating the numerator as the
    # quantity would inflate split orders.
    if re.fullmatch(r"\d+\s*/\s*\d+", text):
        return 1
    number = parse_float(value)
    if number <= 0:
        return 0
    return int(round(number))


def parse_bool(value: Any) -> int:
    text = str(value or "").strip().lower()
    return 1 if text in {"1", "true", "yes", "y", "是", "急", "急单", "紧急"} else 0


def parse_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        y, m, d = match.groups()
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    return None


def normalize_product_type(value: Any) -> str:
    text = str(value or "").strip()
    if "泡沫" in text or "消失" in text:
        return "泡沫实型"
    if "抬" in text:
        return "抬模"
    if "木" in text:
        return "木型"
    return "待确认"


def import_orders(
    file_id: int,
    mapping: FieldMapping,
    *,
    replace_existing: bool = False,
) -> dict[str, Any]:
    with get_conn() as conn:
        file_row = conn.execute("SELECT * FROM upload_files WHERE id = ?", (file_id,)).fetchone()
        if not file_row:
            raise ValueError("上传文件不存在")

        wb = load_workbook(file_row["stored_path"], read_only=True, data_only=True)
        ws = wb[file_row["sheet_name"]]
        header_row = file_row["header_row"]
        raw_headers = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True))
        headers = [str(value).strip() if value is not None else f"空列{idx + 1}" for idx, value in enumerate(raw_headers)]
        header_index = {header: idx for idx, header in enumerate(headers)}

        skipped = 0
        carry_values: dict[str, Any] = {"customer": "", "contract_no": ""}
        parsed_records: list[tuple[int, dict[str, Any]]] = []
        for excel_row, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
            record = row_to_record(row, header_index, mapping, excel_row, carry_values)
            if not record["order_no"]:
                skipped += 1
                continue
            parsed_records.append((excel_row, record))
        if not parsed_records:
            wb.close()
            raise ValueError("没有识别到有效订单，已停止导入，旧订单未被删除")

        removed_orders = 0
        if replace_existing:
            removed_orders = int(conn.execute("SELECT COUNT(*) AS count FROM orders").fetchone()["count"])
            for table in (
                "daily_checkins", "sandbox_usage", "schedule_adjustments",
                "data_issues", "schedule_tasks", "schedule_runs",
                "plan_scope_locks", "plan_locks", "order_progress", "orders",
            ):
                conn.execute(f"DELETE FROM {table}")

        inserted = updated = 0
        for excel_row, record in parsed_records:
            existing = conn.execute("SELECT id FROM orders WHERE order_no = ?", (record["order_no"],)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE orders SET contract_no=?, model_no=?, customer=?, product_name=?,
                        material=?, product_type=?, quantity=?, quantity_raw=?,
                        unit_weight_kg=?, weight_ton=?, molding_method=?,
                        route_json=?, route_source=?, due_date=?, status=?,
                        is_urgent=?, remark=?, source_file=?, source_row=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        record["contract_no"],
                        record["model_no"],
                        record["customer"],
                        record["product_name"],
                        record["material"],
                        record["product_type"],
                        record["quantity"],
                        record["quantity_raw"],
                        record["unit_weight_kg"],
                        record["weight_ton"],
                        record["molding_method"],
                        record["route_json"],
                        record["route_source"],
                        record["due_date"],
                        record["status"],
                        record["is_urgent"],
                        record["remark"],
                        file_row["original_name"],
                        excel_row,
                        now_iso(),
                        existing["id"],
                    ),
                )
                sync_progress(conn, existing["id"], record)
                updated += 1
            else:
                cur = conn.execute(
                    """
                    INSERT INTO orders
                    (order_no, contract_no, model_no, customer, product_name, material,
                     product_type, quantity, quantity_raw, unit_weight_kg,
                     weight_ton, molding_method, route_json, route_source,
                     due_date, status, is_urgent, remark, source_file,
                     source_row, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["order_no"],
                        record["contract_no"],
                        record["model_no"],
                        record["customer"],
                        record["product_name"],
                        record["material"],
                        record["product_type"],
                        record["quantity"],
                        record["quantity_raw"],
                        record["unit_weight_kg"],
                        record["weight_ton"],
                        record["molding_method"],
                        record["route_json"],
                        record["route_source"],
                        record["due_date"],
                        record["status"],
                        record["is_urgent"],
                        record["remark"],
                        file_row["original_name"],
                        excel_row,
                        now_iso(),
                        now_iso(),
                    ),
                )
                sync_progress(conn, cur.lastrowid, record)
                inserted += 1
        wb.close()
        return {
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "replace_existing": replace_existing,
            "removed_orders": removed_orders,
        }


def enrich_existing_orders() -> int:
    """Backfill fields that the legacy importer did not store.

    Legacy generated order numbers end in ``-R<excel row>``. This lets the
    migration recover material, unit weight, product name and route without
    changing the user's existing order identifiers or duplicating orders.
    """
    updated = 0
    with get_conn() as conn:
        files = rows_to_dicts(
            conn.execute(
                "SELECT * FROM upload_files ORDER BY id DESC"
            ).fetchall()
        )
        orders = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM orders
                WHERE source_row IS NULL OR material IS NULL OR unit_weight_kg IS NULL
                   OR model_no IS NULL OR model_no=''
                """
            ).fetchall()
        )
        files_by_name = {row["original_name"]: row for row in files}
        sheet_cache: dict[str, dict[str, Any]] = {}
        for order in orders:
            match = re.search(r"-R(\d+)$", order["order_no"])
            file_row = files_by_name.get(order.get("source_file"))
            if not match or not file_row:
                continue
            path = Path(file_row["stored_path"])
            if not path.exists():
                candidates = list(UPLOAD_DIR.glob(f"*{file_row['original_name']}"))
                if not candidates:
                    continue
                path = candidates[0]
            cache_key = str(path)
            if cache_key not in sheet_cache:
                workbook = load_workbook(
                    path,
                    read_only=True,
                    data_only=True,
                )
                ws = workbook[file_row["sheet_name"]]
                all_rows = {
                    row_number: values
                    for row_number, values in enumerate(
                        ws.iter_rows(values_only=True),
                        start=1,
                    )
                }
                header_values = all_rows.get(int(file_row["header_row"]), ())
                headers = [
                    str(value).strip() if value is not None else f"空列{idx + 1}"
                    for idx, value in enumerate(header_values)
                ]
                sheet_cache[cache_key] = {
                    "workbook": workbook,
                    "rows": all_rows,
                    "header_index": {header: idx for idx, header in enumerate(headers)},
                    "mapping": suggest_mapping(headers),
                }
            cached = sheet_cache[cache_key]
            header_index = cached["header_index"]
            mapping = cached["mapping"]
            row_number = int(match.group(1))
            values = cached["rows"].get(row_number)
            if values is None:
                continue
            def mapped(field: str) -> Any:
                header = mapping.get(field)
                return get_mapped(values, header_index, header)

            unit_weight_kg = parse_float(mapped("weight_kg"))
            quantity_raw = mapped("quantity")
            quantity = parse_quantity(quantity_raw)
            weight_ton = (
                unit_weight_kg * quantity / 1000
                if unit_weight_kg > 0 and quantity > 0
                else float(order.get("weight_ton") or 0)
            )
            molding = normalize_product_type(mapped("molding_method") or mapped("product_type"))
            material = str(mapped("material") or "").strip()
            product_name = str(mapped("product_name") or "").strip()
            model_no = str(mapped("model_no") or "").strip()
            contract_no = str(mapped("order_no") or "").strip()
            due_raw = mapped("due_date")
            route = []
            route_seen = False
            for process in ("打磨", "加工", "检验"):
                header = next((name for name in header_index if process in name), None)
                value = get_mapped(values, header_index, header) if header else None
                if value not in (None, ""):
                    route_seen = True
                if str(value or "").strip() != "——":
                    route.append(process)
            conn.execute(
                """
                UPDATE orders SET contract_no=COALESCE(NULLIF(?, ''), contract_no),
                    model_no=COALESCE(NULLIF(?, ''), model_no),
                    product_name=COALESCE(NULLIF(?, ''), product_name),
                    material=COALESCE(NULLIF(?, ''), material),
                    product_type=CASE WHEN ?='待确认' THEN product_type ELSE ? END,
                    molding_method=CASE WHEN ?='待确认' THEN molding_method ELSE ? END,
                    quantity=CASE WHEN ?>0 THEN ? ELSE quantity END,
                    quantity_raw=?, unit_weight_kg=CASE WHEN ?>0 THEN ? ELSE unit_weight_kg END,
                    weight_ton=CASE WHEN ?>0 THEN ? ELSE weight_ton END,
                    route_json=CASE WHEN ? THEN ? ELSE route_json END,
                    route_source=CASE WHEN ? THEN '订单总表排期列' ELSE route_source END,
                    due_date=COALESCE(?, due_date),
                    status=CASE WHEN ? THEN '暂停' ELSE status END,
                    source_row=?, updated_at=?
                WHERE id=?
                """,
                (
                    contract_no, model_no, product_name, material,
                    molding, molding, molding, molding,
                    quantity, quantity, str(quantity_raw or ""),
                    unit_weight_kg, unit_weight_kg,
                    weight_ton, weight_ton,
                    route_seen, json.dumps(route, ensure_ascii=False),
                    route_seen, parse_date(due_raw), "暂停" in str(due_raw or ""),
                    row_number, now_iso(), order["id"],
                ),
            )
            updated += 1
        for cached in sheet_cache.values():
            cached["workbook"].close()
        conn.execute(
            """
            UPDATE orders SET remark=''
            WHERE TRIM(COALESCE(remark, '')) <> ''
              AND TRIM(remark) = TRIM(COALESCE(product_name, ''))
            """
        )
    return updated


def get_mapped(row: tuple[Any, ...], header_index: dict[str, int], column: str | None) -> Any:
    if not column or column not in header_index:
        return None
    idx = header_index[column]
    return row[idx] if idx < len(row) else None


def row_to_record(
    row: tuple[Any, ...],
    header_index: dict[str, int],
    mapping: FieldMapping,
    excel_row: int,
    carry_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    carry_values = carry_values if carry_values is not None else {}
    contract_no = str(get_mapped(row, header_index, mapping.order_no) or "").strip()
    if contract_no:
        carry_values["contract_no"] = contract_no
    else:
        contract_no = str(carry_values.get("contract_no") or "").strip()

    customer = str(get_mapped(row, header_index, mapping.customer) or "").strip()
    if customer:
        carry_values["customer"] = customer
    else:
        customer = str(carry_values.get("customer") or "").strip()

    model = str(
        get_mapped(row, header_index, mapping.model_no)
        or get_mapped(row, header_index, "铸件型号")
        or get_mapped(row, header_index, "产品代码")
        or ""
    ).strip()
    readable_key = "-".join(part for part in (contract_no, model) if part)
    order_no = f"{readable_key}-R{excel_row}" if readable_key else ""

    quantity_value = get_mapped(row, header_index, mapping.quantity)
    quantity = parse_quantity(quantity_value)
    weight_ton = parse_float(get_mapped(row, header_index, mapping.weight_ton))
    weight_kg = parse_float(get_mapped(row, header_index, mapping.weight_kg))
    if weight_ton <= 0 and weight_kg > 0 and quantity > 0:
        weight_ton = weight_kg * quantity / 1000
    if weight_kg <= 0 and weight_ton > 0 and quantity > 0:
        weight_kg = weight_ton * 1000 / quantity

    raw_due = get_mapped(row, header_index, mapping.due_date)
    status = "暂停" if "暂停" in str(raw_due or "") else "未开始"
    molding_value = get_mapped(
        row,
        header_index,
        mapping.molding_method or mapping.product_type,
    )
    product_type = normalize_product_type(molding_value)
    route: list[str] = []
    route_seen = False
    for process in ("打磨", "加工", "检验"):
        header = next((name for name in header_index if process in name), None)
        value = get_mapped(row, header_index, header) if header else None
        if value not in (None, ""):
            route_seen = True
        if str(value or "").strip() != "——":
            route.append(process)
    remark = str(get_mapped(row, header_index, mapping.remark) or "").strip()
    return {
        "order_no": order_no,
        "contract_no": contract_no,
        "model_no": model,
        "customer": customer,
        "product_name": str(
            get_mapped(row, header_index, mapping.product_name) or ""
        ).strip(),
        "material": str(
            get_mapped(row, header_index, mapping.material) or ""
        ).strip(),
        "product_type": product_type,
        "molding_method": product_type,
        "quantity": quantity,
        "quantity_raw": str(quantity_value or "").strip(),
        "unit_weight_kg": weight_kg or None,
        "weight_ton": weight_ton,
        "due_date": parse_date(raw_due),
        "status": status,
        "route_json": json.dumps(route, ensure_ascii=False) if route_seen else None,
        "route_source": "订单总表排期列" if route_seen else None,
        "remark": remark,
        "is_urgent": parse_bool(get_mapped(row, header_index, mapping.is_urgent))
        or int("急" in remark),
    }


def sync_progress(conn, order_id: int, record: dict[str, Any]) -> None:
    routes = conn.execute(
        "SELECT * FROM process_routes WHERE product_type = ? AND default_required = 1 ORDER BY process_order",
        (record["product_type"],),
    ).fetchall()
    for route in routes:
        planned_qty = calculate_workload(route["workload_formula"], record)
        existing = conn.execute(
            "SELECT completed_qty FROM order_progress WHERE order_id = ? AND process_name = ?",
            (order_id, route["process_name"]),
        ).fetchone()
        completed = min(existing["completed_qty"], planned_qty) if existing else 0
        remaining = max(planned_qty - completed, 0)
        status = "已完成" if remaining <= 0 else ("进行中" if completed > 0 else "未开始")
        conn.execute(
            """
            INSERT INTO order_progress
            (order_id, process_name, planned_qty, completed_qty, remaining_qty, unit, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id, process_name) DO UPDATE SET
                planned_qty=excluded.planned_qty,
                completed_qty=excluded.completed_qty,
                remaining_qty=excluded.remaining_qty,
                unit=excluded.unit,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (order_id, route["process_name"], planned_qty, completed, remaining, route["unit"], status, now_iso()),
        )


def calculate_workload(formula: str, record: dict[str, Any]) -> float:
    if formula == "weight_ton":
        return float(record.get("weight_ton") or 0)
    if formula == "opening_hours":
        return estimate_opening_hours(float(record.get("weight_ton") or 0), float(record.get("quantity") or 0))
    return float(record.get("quantity") or 0)


def estimate_opening_hours(weight_ton: float, quantity: float) -> float:
    if weight_ton <= 0 or quantity <= 0:
        return 0.0
    single_weight_kg = weight_ton * 1000 / quantity
    table = [
        (500, 5), (1000, 15), (1500, 18), (2000, 21), (2500, 24),
        (3000, 27), (3500, 30), (4000, 33), (4500, 36), (5000, 39),
        (6000, 42), (7000, 48), (8000, 54), (9000, 60), (10000, 66),
        (12000, 72), (14000, 84), (16000, 96), (18000, 108), (20000, 120),
        (22000, 132), (24000, 145), (26000, 158), (28000, 171), (30000, 184),
        (32000, 197), (34000, 211), (36000, 225), (38000, 239), (40000, 253),
        (42000, 267), (44000, 282), (46000, 297), (48000, 312), (50000, 327),
    ]
    for upper, hours in table:
        if single_weight_kg <= upper:
            return float(hours)
    extra_steps = (single_weight_kg - 50000) / 2000
    return float(327 + max(0, round(extra_steps)) * 15)

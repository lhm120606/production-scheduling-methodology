from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parents[2]
APP_BASE_DIR = Path(os.environ.get("GUOGANG_BASE_DIR", BASE_DIR))
DATA_ROOT = Path(os.environ.get("GUOGANG_DATA_DIR", BASE_DIR / "work"))
DATA_DIR = DATA_ROOT / "data"
UPLOAD_DIR = DATA_ROOT / "uploads"
DB_PATH = DATA_DIR / "guogang_schedule.db"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn() -> Iterable[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def execute_many(conn: sqlite3.Connection, sql: str, rows: list[tuple[Any, ...]]) -> None:
    if rows:
        conn.executemany(sql, rows)


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def init_db() -> None:
    ensure_dirs()
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS upload_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                sheet_name TEXT,
                header_row INTEGER,
                uploaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL UNIQUE,
                contract_no TEXT,
                model_no TEXT,
                customer TEXT,
                product_name TEXT,
                material TEXT,
                product_type TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 0,
                quantity_raw TEXT,
                unit_weight_kg REAL,
                weight_ton REAL NOT NULL DEFAULT 0,
                completed_quantity INTEGER NOT NULL DEFAULT 0,
                molding_method TEXT,
                route_json TEXT,
                route_source TEXT,
                length_mm REAL,
                width_mm REAL,
                height_mm REAL,
                sandbox_id INTEGER,
                sandbox_plan TEXT,
                due_date TEXT,
                status TEXT NOT NULL DEFAULT '未开始',
                is_urgent INTEGER NOT NULL DEFAULT 0,
                is_locked INTEGER NOT NULL DEFAULT 0,
                remark TEXT,
                source_file TEXT,
                source_row INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS process_routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_type TEXT NOT NULL,
                process_name TEXT NOT NULL,
                process_order INTEGER NOT NULL,
                unit TEXT NOT NULL,
                workload_formula TEXT NOT NULL,
                default_required INTEGER NOT NULL DEFAULT 1,
                UNIQUE(product_type, process_name)
            );

            CREATE TABLE IF NOT EXISTS order_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                process_name TEXT NOT NULL,
                planned_qty REAL NOT NULL DEFAULT 0,
                completed_qty REAL NOT NULL DEFAULT 0,
                remaining_qty REAL NOT NULL DEFAULT 0,
                unit TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '未开始',
                updated_at TEXT NOT NULL,
                UNIQUE(order_id, process_name),
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS capacity_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                process_name TEXT NOT NULL UNIQUE,
                daily_capacity REAL NOT NULL,
                unit TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                remark TEXT
            );

            CREATE TABLE IF NOT EXISTS capacity_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                process_name TEXT NOT NULL,
                capacity REAL NOT NULL,
                unit TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                UNIQUE(date, process_name)
            );

            CREATE TABLE IF NOT EXISTS capacity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                process_name TEXT NOT NULL,
                old_capacity REAL,
                new_capacity REAL NOT NULL,
                unit TEXT NOT NULL,
                change_type TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedule_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_version TEXT NOT NULL UNIQUE,
                run_time TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                summary TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedule_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_version TEXT NOT NULL,
                date TEXT NOT NULL,
                order_id INTEGER NOT NULL,
                order_no TEXT NOT NULL,
                contract_no TEXT,
                model_no TEXT,
                customer TEXT,
                product_name TEXT,
                material TEXT,
                product_type TEXT,
                process_name TEXT NOT NULL,
                planned_qty REAL NOT NULL,
                unit TEXT NOT NULL,
                priority_level TEXT NOT NULL,
                due_date TEXT,
                status TEXT NOT NULL DEFAULT '计划',
                rolled_over INTEGER NOT NULL DEFAULT 0,
                is_urgent INTEGER NOT NULL DEFAULT 0,
                sandbox_id INTEGER,
                sandbox_name TEXT,
                sandbox_status TEXT,
                sandbox_note TEXT,
                planned_units INTEGER,
                planned_weight_ton REAL,
                unit_weight_kg REAL,
                order_quantity INTEGER,
                completed_units_before_plan INTEGER,
                order_remark TEXT,
                estimated_hours REAL,
                quantity_issue TEXT,
                related_casting_date TEXT,
                mandatory_finish_at TEXT,
                decision_reason TEXT,
                risk_status TEXT,
                is_locked INTEGER NOT NULL DEFAULT 0,
                batch_key TEXT,
                week_start TEXT,
                week_end TEXT,
                iso_week TEXT,
                task_nature TEXT,
                latest_finish_date TEXT,
                is_cross_week INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS daily_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_key TEXT,
                date TEXT NOT NULL,
                task_id INTEGER,
                order_id INTEGER NOT NULL,
                process_name TEXT NOT NULL,
                planned_qty REAL NOT NULL,
                actual_qty REAL NOT NULL,
                planned_units INTEGER,
                actual_units INTEGER,
                status TEXT NOT NULL,
                reason TEXT,
                remark TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sandboxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sandbox_type TEXT NOT NULL DEFAULT '普通',
                source_location TEXT,
                source_file TEXT,
                dimension_text TEXT,
                inner_length_mm REAL NOT NULL,
                inner_width_mm REAL NOT NULL,
                inner_height_mm REAL NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                has_belt INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '可用',
                available_from TEXT,
                remark TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sandbox_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_version TEXT NOT NULL,
                date TEXT NOT NULL,
                order_id INTEGER NOT NULL,
                order_no TEXT NOT NULL,
                process_name TEXT NOT NULL,
                sandbox_id INTEGER,
                sandbox_name TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY(sandbox_id) REFERENCES sandboxes(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS drawing_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                extracted_text TEXT,
                length_mm REAL,
                width_mm REAL,
                height_mm REAL,
                matched_sandbox_id INTEGER,
                fit_message TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS opening_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                min_weight_kg REAL NOT NULL,
                max_weight_kg REAL NOT NULL,
                min_hours REAL NOT NULL,
                max_hours REAL NOT NULL,
                material_pattern TEXT,
                source TEXT NOT NULL,
                UNIQUE(min_weight_kg, max_weight_kg, material_pattern)
            );

            CREATE TABLE IF NOT EXISTS data_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_version TEXT,
                order_id INTEGER,
                order_no TEXT,
                issue_code TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '待确认',
                created_at TEXT NOT NULL,
                UNIQUE(schedule_version, order_id, issue_code)
            );

            CREATE TABLE IF NOT EXISTS schedule_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_version TEXT NOT NULL,
                previous_version TEXT,
                order_id INTEGER,
                order_no TEXT,
                process_name TEXT,
                before_date TEXT,
                after_date TEXT,
                before_units INTEGER,
                after_units INTEGER,
                planned_units INTEGER,
                reason TEXT NOT NULL,
                triggered_by_urgent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS plan_locks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                process_name TEXT NOT NULL,
                locked_date TEXT NOT NULL,
                planned_units INTEGER,
                reason TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(order_id, process_name)
            );

            CREATE TABLE IF NOT EXISTS plan_scope_locks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type TEXT NOT NULL,
                scope_value TEXT NOT NULL,
                process_name TEXT,
                source_schedule_version TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(scope_type, scope_value, process_name)
            );
            """
        )
        ensure_columns(conn, "orders", {
            "contract_no": "TEXT",
            "model_no": "TEXT",
            "product_name": "TEXT",
            "material": "TEXT",
            "quantity_raw": "TEXT",
            "unit_weight_kg": "REAL",
            "completed_quantity": "INTEGER NOT NULL DEFAULT 0",
            "molding_method": "TEXT",
            "route_json": "TEXT",
            "route_source": "TEXT",
            "is_locked": "INTEGER NOT NULL DEFAULT 0",
            "source_row": "INTEGER",
        })
        ensure_columns(conn, "orders", {
            "length_mm": "REAL",
            "width_mm": "REAL",
            "height_mm": "REAL",
            "sandbox_id": "INTEGER",
            "sandbox_plan": "TEXT",
        })
        ensure_columns(conn, "schedule_tasks", {
            "sandbox_id": "INTEGER",
            "sandbox_name": "TEXT",
            "sandbox_status": "TEXT",
            "sandbox_note": "TEXT",
            "planned_units": "INTEGER",
            "planned_weight_ton": "REAL",
            "estimated_hours": "REAL",
            "quantity_issue": "TEXT",
            "related_casting_date": "TEXT",
            "mandatory_finish_at": "TEXT",
            "decision_reason": "TEXT",
            "risk_status": "TEXT",
            "is_locked": "INTEGER NOT NULL DEFAULT 0",
            "batch_key": "TEXT",
            "contract_no": "TEXT",
            "model_no": "TEXT",
            "product_name": "TEXT",
            "material": "TEXT",
            "unit_weight_kg": "REAL",
            "order_quantity": "INTEGER",
            "completed_units_before_plan": "INTEGER NOT NULL DEFAULT 0",
            "order_remark": "TEXT",
            "week_start": "TEXT",
            "week_end": "TEXT",
            "iso_week": "TEXT",
            "task_nature": "TEXT",
            "latest_finish_date": "TEXT",
            "is_cross_week": "INTEGER NOT NULL DEFAULT 0",
        })
        ensure_columns(conn, "schedule_adjustments", {
            "before_units": "INTEGER",
            "after_units": "INTEGER",
            "triggered_by_urgent": "INTEGER NOT NULL DEFAULT 0",
        })
        ensure_columns(conn, "daily_checkins", {
            "business_key": "TEXT",
            "planned_units": "INTEGER",
            "actual_units": "INTEGER",
        })
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_checkins_business_key "
            "ON daily_checkins(business_key)"
        )
        ensure_columns(conn, "sandboxes", {
            "sandbox_type": "TEXT NOT NULL DEFAULT '普通'",
            "source_location": "TEXT",
            "source_file": "TEXT",
            "dimension_text": "TEXT",
            "has_belt": "INTEGER NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT '可用'",
            "available_from": "TEXT",
        })
        seed_defaults(conn)


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def seed_defaults(conn: sqlite3.Connection) -> None:
    process_units = {
        "木模造型": ("吨", 15),
        "消失模造型": ("吨", 10),
        "浇筑": ("吨", 25),
        "开箱": ("小时", 24),
        "打磨": ("件", 30),
        "加工": ("件", 20),
        "检验": ("件", 35),
    }
    for process, (unit, capacity) in process_units.items():
        conn.execute(
            """
            INSERT INTO capacity_config
            (process_name, daily_capacity, unit, effective_from, remark)
            VALUES (?, ?, ?, date('now'), ?)
            ON CONFLICT(process_name) DO NOTHING
            """,
            (process, capacity, unit, "按当前车间产能初始化：浇筑23-27T取25T，木模15T，消失模10T，开箱按重量小时表"),
        )

    route_by_type = {
        "泡沫实型": [
            ("消失模造型", 1, "吨", "weight_ton"),
            ("浇筑", 2, "吨", "weight_ton"),
            ("开箱", 3, "小时", "opening_hours"),
            ("打磨", 4, "件", "quantity"),
            ("加工", 5, "件", "quantity"),
            ("检验", 6, "件", "quantity"),
        ],
        "抬模": [
            ("木模造型", 1, "吨", "weight_ton"),
            ("浇筑", 2, "吨", "weight_ton"),
            ("开箱", 3, "小时", "opening_hours"),
            ("打磨", 4, "件", "quantity"),
            ("加工", 5, "件", "quantity"),
            ("检验", 6, "件", "quantity"),
        ],
        "木型": [
            ("木模造型", 1, "吨", "weight_ton"),
            ("浇筑", 2, "吨", "weight_ton"),
            ("开箱", 3, "小时", "opening_hours"),
            ("打磨", 4, "件", "quantity"),
            ("加工", 5, "件", "quantity"),
            ("检验", 6, "件", "quantity"),
        ],
    }
    for product_type, routes in route_by_type.items():
        for process, order, unit, formula in routes:
            conn.execute(
                """
                INSERT INTO process_routes
                (product_type, process_name, process_order, unit, workload_formula, default_required)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(product_type, process_name) DO UPDATE SET
                    process_order=excluded.process_order,
                    unit=excluded.unit,
                    workload_formula=excluded.workload_formula,
                    default_required=excluded.default_required
                """,
                (product_type, process, order, unit, formula),
            )

    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, updated_at)
        VALUES ('ai_config', ?, ?)
        """,
        (json_dumps({"enabled": False, "apiKey": "", "modelName": "", "baseUrl": ""}), now_iso()),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, updated_at)
        VALUES ('scheduler_config', ?, ?)
        """,
        (
            json_dumps(
                {
                    "horizon_days": 30,
                    "working_weekdays": [0, 1, 2, 3, 4, 5, 6],
                    "molding_lead_days": 1,
                    "casting_target_ton": 25,
                    "casting_normal_min_ton": 23,
                    "casting_normal_max_ton": 27,
                    "casting_absolute_max_ton": 27,
                    "qt_furnace_min_ton": 3,
                    "qt_furnace_max_ton": 4,
                }
            ),
            now_iso(),
        ),
    )
    seed_opening_rules(conn)


def seed_opening_rules(conn: sqlite3.Connection) -> None:
    ranges = [
        (100, 500, 6, 15), (501, 1000, 15, 18), (1001, 1500, 18, 21),
        (1501, 2000, 21, 24), (2001, 2500, 24, 27), (2501, 3000, 27, 30),
        (3001, 3500, 30, 33), (3501, 4000, 33, 36), (4001, 4500, 36, 39),
        (4501, 5000, 39, 42), (5001, 6000, 42, 48), (6001, 7000, 48, 54),
        (7001, 8000, 54, 60), (8001, 9000, 60, 66), (9001, 10000, 66, 72),
        (10001, 12000, 72, 84), (12001, 14000, 84, 96), (14001, 16000, 96, 108),
        (16001, 18000, 108, 120), (18001, 20000, 120, 132), (20001, 22000, 132, 145),
        (22001, 24000, 145, 158), (24001, 26000, 158, 171), (26001, 28000, 171, 184),
        (28001, 30000, 184, 197), (30001, 32000, 197, 211), (32001, 34000, 211, 225),
        (34001, 36000, 225, 239), (36001, 38000, 239, 253), (38001, 40000, 253, 267),
        (40001, 42000, 267, 282), (42001, 44000, 282, 297), (44001, 46000, 297, 312),
        (46001, 48000, 312, 327), (48001, 50000, 327, 342),
    ]
    for minimum, maximum, min_hours, max_hours in ranges:
        conn.execute(
            """
            INSERT OR IGNORE INTO opening_rules
            (min_weight_kg, max_weight_kg, min_hours, max_hours, material_pattern, source)
            VALUES (?, ?, ?, ?, NULL, '开箱时间工作表')
            """,
            (minimum, maximum, min_hours, max_hours),
        )

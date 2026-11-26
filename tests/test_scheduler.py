from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch


TEST_ROOT = Path(tempfile.mkdtemp(prefix="guogang-tests-"))
os.environ["GUOGANG_DATA_DIR"] = str(TEST_ROOT / "work")
os.environ["GUOGANG_BASE_DIR"] = str(TEST_ROOT)

from openpyxl import load_workbook

from backend.app.db import get_conn, init_db, now_iso
from backend.app.excel_import import parse_quantity, suggest_mapping, sync_progress
from backend.app.exporter import export_full
from backend.app.main import app, checkin
from backend.app.models import CheckinRequest, OrderPayload
from backend.app.scheduler import (
    build_deadline_alerts,
    daily_reminder_data,
    dashboard_data,
    generate_schedule,
    reverse_process_deadlines,
    week_bounds,
)
from backend.app.rules import production_days, scheduler_config


class SchedulerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        with get_conn() as conn:
            for table in (
                "daily_checkins", "sandbox_usage", "schedule_adjustments",
                "data_issues", "schedule_tasks", "schedule_runs",
                "plan_scope_locks", "plan_locks", "order_progress", "orders",
            ):
                conn.execute(f"DELETE FROM {table}")

    def add_order(
        self,
        order_no: str,
        *,
        quantity: int,
        unit_weight_kg: float | None,
        product_type: str = "木型",
        material: str = "HT300",
        due_days: int = 30,
        urgent: bool = False,
        status: str = "未开始",
        route: str = '["打磨","加工","检验"]',
    ) -> int:
        weight_ton = (
            quantity * unit_weight_kg / 1000
            if quantity > 0 and unit_weight_kg
            else (3.5 if quantity == 0 else 0)
        )
        with get_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders
                (order_no, customer, product_name, material, product_type,
                 molding_method, quantity, unit_weight_kg, weight_ton,
                 route_json, route_source, due_date, status, is_urgent,
                 remark, source_file, created_at, updated_at)
                VALUES (?, '测试客户', '测试铸件', ?, ?, ?, ?, ?, ?, ?,
                        '测试路线', ?, ?, ?, '', '测试', ?, ?)
                """,
                (
                    order_no, material, product_type, product_type, quantity,
                    unit_weight_kg, weight_ton, route,
                    (date.today() + timedelta(days=due_days)).isoformat(),
                    status, int(urgent), now_iso(), now_iso(),
                ),
            )
            record = {
                "product_type": product_type,
                "quantity": quantity,
                "weight_ton": weight_ton,
            }
            sync_progress(conn, cursor.lastrowid, record)
            return int(cursor.lastrowid)

    def latest_tasks(self, process: str | None = None) -> list[dict]:
        with get_conn() as conn:
            version = conn.execute(
                "SELECT schedule_version FROM schedule_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()["schedule_version"]
            sql = "SELECT * FROM schedule_tasks WHERE schedule_version=?"
            params: list[object] = [version]
            if process:
                sql += " AND process_name=?"
                params.append(process)
            sql += " ORDER BY date, order_id, id"
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def test_normal_plan_is_casting_first_and_molding_points_to_casting(self) -> None:
        self.add_order("NORMAL-1", quantity=14, unit_weight_kg=250)
        generate_schedule("test")
        casting = self.latest_tasks("浇筑")
        molding = self.latest_tasks("木模造型")
        opening = self.latest_tasks("开箱")
        self.assertTrue(casting)
        self.assertTrue(molding)
        self.assertEqual(sum(row["planned_units"] for row in casting), 14)
        self.assertTrue(all(row["related_casting_date"] for row in molding))
        self.assertTrue(all(row["date"] < row["related_casting_date"] for row in molding))
        self.assertEqual(opening[0]["planned_units"], 14)
        self.assertEqual(opening[0]["estimated_hours"], 15)
        self.assertEqual(opening[0]["planned_weight_ton"], 3.5)

    def test_updated_order_sheet_headers_map_to_exact_business_fields(self) -> None:
        headers = [
            "客户名称", "合同号/订单号", "产品代码", "铸件型号",
            "铸件名称", "材质", "件数", "单重（KG）", "交货日期",
            "工艺类型\n（下拉）",
        ]
        mapping = suggest_mapping(headers)
        self.assertEqual(mapping["order_no"], "合同号/订单号")
        self.assertEqual(mapping["model_no"], "铸件型号")
        self.assertEqual(mapping["product_name"], "铸件名称")
        self.assertEqual(mapping["customer"], "客户名称")
        self.assertEqual(mapping["quantity"], "件数")

    def test_split_batches_keep_integer_units_and_batch_tonnage(self) -> None:
        self.add_order("SPLIT-1", quantity=100, unit_weight_kg=800)
        generate_schedule("test")
        casting = self.latest_tasks("浇筑")
        self.assertEqual(sum(row["planned_units"] for row in casting), 100)
        for row in casting:
            self.assertIsInstance(row["planned_units"], int)
            self.assertAlmostEqual(
                row["planned_weight_ton"],
                row["planned_units"] * 0.8,
                places=6,
            )
        daily = {}
        for row in casting:
            daily[row["date"]] = daily.get(row["date"], 0) + row["planned_weight_ton"]
        self.assertTrue(all(value <= 27.000001 for value in daily.values()))

    def test_partial_completion_only_plans_remaining_units(self) -> None:
        order_id = self.add_order("PARTIAL-1", quantity=10, unit_weight_kg=500)
        with get_conn() as conn:
            for process in ("木模造型", "浇筑"):
                conn.execute(
                    """
                    UPDATE order_progress
                    SET completed_qty=2.5, remaining_qty=2.5, status='部分完成'
                    WHERE order_id=? AND process_name=?
                    """,
                    (order_id, process),
                )
        generate_schedule("test")
        casting = self.latest_tasks("浇筑")
        self.assertEqual(sum(row["planned_units"] for row in casting), 5)
        self.assertEqual(sum(row["planned_weight_ton"] for row in casting), 2.5)

    def test_urgent_precedes_normal_and_paused_is_excluded(self) -> None:
        normal_id = self.add_order("NORMAL-2", quantity=20, unit_weight_kg=1000)
        urgent_id = self.add_order("URGENT-1", quantity=4, unit_weight_kg=1000, urgent=True)
        paused_id = self.add_order("PAUSED-1", quantity=2, unit_weight_kg=1000, status="暂停")
        with get_conn() as conn:
            for order_id in (normal_id, urgent_id):
                conn.execute(
                    "UPDATE order_progress SET completed_qty=planned_qty, remaining_qty=0 WHERE order_id=? AND process_name='木模造型'",
                    (order_id,),
                )
        generate_schedule("test")
        casting = self.latest_tasks("浇筑")
        dates_by_order = {row["order_id"]: row["date"] for row in casting}
        self.assertLessEqual(dates_by_order[urgent_id], dates_by_order[normal_id])
        self.assertNotIn(paused_id, {row["order_id"] for row in casting})

    def test_qt_rule_and_opening_lower_bound(self) -> None:
        self.add_order("QT-1", quantity=8, unit_weight_kg=500, material="QT500-7")
        generate_schedule("test")
        casting = self.latest_tasks("浇筑")
        opening = self.latest_tasks("开箱")
        self.assertTrue(all(row["planned_weight_ton"] <= 4.000001 for row in casting))
        self.assertTrue(all(row["estimated_hours"] == 6 for row in opening))

    def test_missing_weight_and_missing_units_become_issues(self) -> None:
        self.add_order("NO-WEIGHT", quantity=14, unit_weight_kg=None)
        self.add_order("NO-UNITS", quantity=0, unit_weight_kg=500)
        generate_schedule("test")
        with get_conn() as conn:
            codes = {
                row["issue_code"]
                for row in conn.execute(
                    "SELECT issue_code FROM data_issues WHERE schedule_version=(SELECT schedule_version FROM schedule_runs ORDER BY id DESC LIMIT 1)"
                ).fetchall()
            }
        self.assertIn("UNIT_WEIGHT_MISSING", codes)
        self.assertIn("PLAN_UNITS_MISSING", codes)

    def test_locked_casting_date_is_preserved(self) -> None:
        order_id = self.add_order("LOCKED-1", quantity=2, unit_weight_kg=1000)
        locked_date = date.today() + timedelta(days=4)
        while locked_date.weekday() == 6:
            locked_date += timedelta(days=1)
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO plan_locks
                (order_id, process_name, locked_date, planned_units, reason, created_at)
                VALUES (?, '浇筑', ?, 2, '人工确认', ?)
                """,
                (order_id, locked_date.isoformat(), now_iso()),
            )
        generate_schedule("test")
        casting = self.latest_tasks("浇筑")
        self.assertEqual({row["date"] for row in casting}, {locked_date.isoformat()})
        self.assertTrue(all(row["is_locked"] == 1 for row in casting))

    def test_same_input_is_deterministic(self) -> None:
        self.add_order("DETERMINISTIC", quantity=25, unit_weight_kg=800)
        generate_schedule("first")
        first = [
            (row["date"], row["process_name"], row["order_id"], row["planned_units"])
            for row in self.latest_tasks()
        ]
        generate_schedule("second")
        second = [
            (row["date"], row["process_name"], row["order_id"], row["planned_units"])
            for row in self.latest_tasks()
        ]
        self.assertEqual(first, second)

    def test_checkin_is_idempotent(self) -> None:
        self.add_order("CHECKIN-1", quantity=2, unit_weight_kg=500)
        generate_schedule("test")
        task = self.latest_tasks()[0]
        payload = {
            "date": task["date"],
            "idempotency_key": "same-checkin",
            "items": [{
                "task_id": task["id"],
                "order_id": task["order_id"],
                "process_name": task["process_name"],
                "planned_qty": task["planned_qty"],
                "actual_qty": task["planned_qty"],
                "planned_units": task["planned_units"],
                "actual_units": task["planned_units"],
                "status": "完成",
                "reason": "",
                "remark": "",
            }],
        }
        first = checkin(CheckinRequest.model_validate(payload))
        second = checkin(CheckinRequest.model_validate(payload))
        self.assertEqual(first["checkin"]["accepted"], 1)
        self.assertEqual(second["checkin"]["duplicates"], 1)

    def test_export_matches_api_plan_fields(self) -> None:
        self.add_order("EXPORT-1", quantity=14, unit_weight_kg=250)
        generate_schedule("test")
        first_casting = self.latest_tasks("浇筑")[0]
        selected_week, _ = week_bounds(date.fromisoformat(first_casting["date"]))
        dashboard = dashboard_data(selected_week)
        path = export_full(selected_week)
        workbook = load_workbook(path, read_only=True, data_only=True)
        required = {
            "周生产计划",
            "本周任务总表", "本周浇筑主计划", "本周必须造型任务",
            "下周浇筑前置任务", "本周开箱任务", "本周后段任务",
            "按日期周计划", "按订单周计划", "按工序周计划",
            "未来4周负载", "周计划风险清单", "数据异常清单",
            "延期和顺延任务", "计划调整记录", "系统规则摘要",
        }
        self.assertEqual(required, set(workbook.sheetnames))
        week_ws = workbook["周生产计划"]
        week_headers = [
            cell.value for cell in next(week_ws.iter_rows(min_row=2, max_row=2))
        ]
        self.assertIn("本周计划数量", week_headers)
        self.assertIn("本周工序", week_headers)
        self.assertNotIn("计划日期", week_headers)
        ws = workbook["本周浇筑主计划"]
        headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
        self.assertIn("计划件数", headers)
        self.assertIn("计划吨位", headers)
        first_row = [cell.value for cell in next(ws.iter_rows(min_row=2, max_row=2))]
        units = first_row[headers.index("计划件数")]
        ton = first_row[headers.index("计划吨位")]
        self.assertIsInstance(units, int)
        self.assertIsInstance(ton, (int, float))
        self.assertEqual(len(dashboard["casting_load"]), 30)
        self.assertEqual(len(dashboard["weekly_casting_days"]), 7)
        self.assertEqual(len(dashboard["four_week_load"]), 4)
        workbook.close()

    def test_cross_week_molding_is_in_current_week_plan(self) -> None:
        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 7, 28)

        with patch(f"{__name__}.date", FixedDate), patch("backend.app.scheduler.date", FixedDate):
            self._assert_cross_week_molding()

    def _assert_cross_week_molding(self) -> None:
        self.add_order("CROSS-WEEK", quantity=1, unit_weight_kg=1000, due_days=10)
        generate_schedule("test")
        casting = self.latest_tasks("浇筑")
        self.assertTrue(casting)
        casting_day = date.fromisoformat(casting[0]["date"])
        self.assertEqual(casting_day.weekday(), 0)
        casting_week, _ = week_bounds(casting_day)
        dashboard = dashboard_data(casting_week - timedelta(days=7))
        prep = dashboard["next_week_prep_molding"]
        self.assertTrue(prep)
        self.assertTrue(all(date.fromisoformat(row["date"]).weekday() == 6 for row in prep))
        self.assertTrue(all(row["is_cross_week"] == 1 for row in prep))
        self.assertTrue(all(
            row["task_nature"] == "下周浇筑前置任务" for row in prep
        ))

    def test_deadline_alerts_use_real_casting_countdown_and_group_language(self) -> None:
        as_of = date(2026, 7, 29)
        alerts = build_deadline_alerts(
            [
                {
                    "id": 1,
                    "order_id": 1,
                    "order_no": "ALERT-1",
                    "product_name": "机架",
                    "process_name": "木模造型",
                    "planned_units": 12,
                    "planned_weight_ton": 3.5,
                    "latest_finish_date": "2026-07-31",
                    "related_casting_date": "2026-08-03",
                },
                {
                    "id": 2,
                    "order_id": 2,
                    "order_no": "ALERT-OVERDUE",
                    "product_name": "底座",
                    "process_name": "消失模造型",
                    "planned_units": 4,
                    "planned_weight_ton": 2,
                    "latest_finish_date": "2026-07-28",
                    "related_casting_date": "2026-07-31",
                },
                {
                    "id": 3,
                    "order_id": 3,
                    "order_no": "TOO-EARLY",
                    "product_name": "远期任务",
                    "process_name": "木模造型",
                    "planned_units": 1,
                    "planned_weight_ton": 1,
                    "latest_finish_date": "2026-08-08",
                    "related_casting_date": "2026-08-10",
                },
            ],
            as_of,
        )
        self.assertEqual(2, len(alerts))
        overdue, warning = alerts
        self.assertEqual("overdue", overdue["level"])
        self.assertNotIn("逾期", overdue["message"])
        self.assertNotIn("还有", overdue["message"])
        self.assertIn("消失模造型组", overdue["message"])
        self.assertIn("最晚完成时间为7月28日", overdue["message"])
        self.assertEqual(5, warning["days_to_casting"])
        self.assertEqual(2, warning["days_to_deadline"])
        self.assertNotIn("还有5天", warning["message"])
        self.assertIn("木模造型组须完成订单 ALERT-1 的机架木模造型任务12件（3.50吨）", warning["message"])
        self.assertIn("最晚完成时间为7月31日", warning["message"])
        self.assertIn("计划浇筑时间为8月3日", warning["message"])

    def test_daily_reminder_module_is_backed_by_current_schedule(self) -> None:
        self.add_order("REMINDER-1", quantity=30, unit_weight_kg=500)
        generate_schedule("test")
        reminders = daily_reminder_data()
        self.assertIn("summary", reminders)
        self.assertIn("group_summary", reminders)
        self.assertIn("reminders", reminders)
        self.assertEqual(
            reminders["summary"]["total"],
            len(reminders["reminders"]),
        )
        self.assertTrue(all(row["message"] for row in reminders["reminders"]))

    def test_week_plan_can_select_next_and_following_weeks(self) -> None:
        self.add_order("MULTI-WEEK", quantity=500, unit_weight_kg=500)
        generate_schedule("test")
        current_start, _ = week_bounds(date.today())
        next_start = current_start + timedelta(days=7)
        following_start = current_start + timedelta(days=14)

        next_week = dashboard_data(next_start)
        following_week = dashboard_data(following_start)

        self.assertEqual("下周", next_week["week_relation"])
        self.assertEqual(next_start.isoformat(), next_week["week_start"])
        self.assertTrue(next_week["week_tasks"])
        self.assertTrue(all(
            next_week["week_start"] <= row["date"] <= next_week["week_end"]
            for row in next_week["week_tasks"]
        ))
        self.assertEqual("下下周", following_week["week_relation"])
        self.assertEqual(following_start.isoformat(), following_week["week_start"])
        available = {
            row["week_start"] for row in following_week["available_weeks"]
        }
        self.assertIn(current_start.isoformat(), available)
        self.assertIn(next_start.isoformat(), available)
        self.assertIn(following_start.isoformat(), available)

    def test_selected_week_export_matches_selected_week_api(self) -> None:
        self.add_order("EXPORT-NEXT-WEEK", quantity=500, unit_weight_kg=500)
        generate_schedule("test")
        current_start, _ = week_bounds(date.today())
        selected = current_start + timedelta(days=7)
        dashboard = dashboard_data(selected)
        path = export_full(selected)
        workbook = load_workbook(path, read_only=True, data_only=True)
        ws = workbook["本周任务总表"]
        self.assertEqual(len(dashboard["week_tasks"]), ws.max_row - 1)
        headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
        date_column = headers.index("计划日期")
        dates = [
            row[date_column].value
            for row in ws.iter_rows(min_row=2)
            if row[date_column].value
        ]
        self.assertTrue(dates)
        self.assertTrue(all(
            dashboard["week_start"] <= value <= dashboard["week_end"]
            for value in dates
        ))
        workbook.close()

    def test_large_order_has_stable_piece_ranges_and_batch_numbers(self) -> None:
        self.add_order("MODEL-LARGE-R500", quantity=100, unit_weight_kg=800)
        generate_schedule("test")
        first_casting = self.latest_tasks("浇筑")[0]
        dashboard = dashboard_data(date.fromisoformat(first_casting["date"]))
        casting = [row for row in dashboard["week_tasks"] if row["process_name"] == "浇筑"]
        self.assertTrue(casting)
        self.assertTrue(all(row["production_batch_no"].startswith("JZ") for row in casting))
        self.assertTrue(all(row["piece_start"] <= row["piece_end"] for row in casting))
        self.assertTrue(all("共100件" in row["piece_label"] for row in casting))
        self.assertEqual("MODEL-LARGE", casting[0]["model_no"])
        self.assertEqual(100 - casting[0]["piece_end"], casting[0]["remaining_order_units"])
        molding_by_batch = {
            row["batch_key"]: row
            for row in dashboard["week_tasks"]
            if row["process_name"].endswith("造型") and row.get("batch_key")
        }
        first_molding = molding_by_batch.get(casting[0]["batch_key"])
        if first_molding:
            self.assertEqual(casting[0]["piece_end"], first_molding["piece_end"])

    def test_sandbox_data_does_not_block_or_create_schedule_issue(self) -> None:
        self.add_order("NO-SANDBOX", quantity=4, unit_weight_kg=500)
        generate_schedule("test")
        self.assertTrue(self.latest_tasks("浇筑"))
        with get_conn() as conn:
            version = conn.execute(
                "SELECT schedule_version FROM schedule_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()["schedule_version"]
            sandbox_issue_count = conn.execute(
                """
                SELECT COUNT(*) AS count FROM data_issues
                WHERE schedule_version=? AND issue_code LIKE 'SANDBOX%'
                """,
                (version,),
            ).fetchone()["count"]
            usage_count = conn.execute(
                "SELECT COUNT(*) AS count FROM sandbox_usage WHERE schedule_version=?",
                (version,),
            ).fetchone()["count"]
        self.assertEqual(sandbox_issue_count, 0)
        self.assertEqual(usage_count, 0)

    def test_removed_resource_module_is_not_exposed(self) -> None:
        public_paths = {route.path for route in app.routes}
        self.assertFalse(any(path.startswith("/api/sandboxes") for path in public_paths))
        self.assertNotIn("/api/drawings/analyze", public_paths)
        order_fields = set(OrderPayload.model_fields)
        self.assertTrue({"order_no", "quantity", "weight_ton", "due_date"} <= order_fields)
        self.assertTrue({"length_mm", "width_mm", "height_mm", "sandbox_id", "sandbox_plan"}.isdisjoint(order_fields))

    def test_week_scope_lock_preserves_casting_tasks(self) -> None:
        self.add_order("WEEK-LOCK", quantity=30, unit_weight_kg=500)
        first = generate_schedule("first")
        first_casting = self.latest_tasks("浇筑")[0]
        selected_week, _ = week_bounds(date.fromisoformat(first_casting["date"]))
        dashboard = dashboard_data(selected_week)
        before = [
            (row["date"], row["order_id"], row["planned_units"])
            for row in dashboard["week_casting"]
        ]
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO plan_scope_locks
                (scope_type, scope_value, process_name, source_schedule_version,
                 reason, created_at)
                VALUES ('week', ?, '浇筑', ?, '测试整周锁定', ?)
                """,
                (dashboard["week_start"], first["schedule_version"], now_iso()),
            )
        generate_schedule("week_lock")
        after = [
            (row["date"], row["order_id"], row["planned_units"])
            for row in dashboard_data(selected_week)["week_casting"]
        ]
        self.assertEqual(before, after)
        locked_rows = [
            row for row in self.latest_tasks("浇筑")
            if dashboard["week_start"] <= row["date"] <= dashboard["week_end"]
        ]
        self.assertTrue(locked_rows)
        self.assertTrue(all(row["is_locked"] == 1 for row in locked_rows))

    def test_all_seven_weekdays_are_production_days(self) -> None:
        with get_conn() as conn:
            config = scheduler_config(conn)
        self.assertEqual(set(range(7)), set(config["working_weekdays"]))
        saturday = date(2026, 8, 15)
        self.assertEqual(
            [date(2026, 8, 15), date(2026, 8, 16), date(2026, 8, 17)],
            production_days(saturday, 3, config),
        )

    def test_process_deadlines_are_reversed_from_due_date(self) -> None:
        order_id = self.add_order("DUE-BACKWARD", quantity=1, unit_weight_kg=500, due_days=20)
        with get_conn() as conn:
            order = dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
            deadlines = reverse_process_deadlines(conn, order, scheduler_config(conn))
        self.assertEqual(date.fromisoformat(order["due_date"]), deadlines["检验"])
        self.assertLess(deadlines["加工"], deadlines["检验"])
        self.assertLess(deadlines["打磨"], deadlines["加工"])
        self.assertLess(deadlines["开箱"], deadlines["打磨"])
        self.assertLess(deadlines["浇筑"], deadlines["开箱"])
        self.assertLess(deadlines["木模造型"], deadlines["浇筑"])

    def test_quantity_fraction_is_one_piece(self) -> None:
        self.assertEqual(parse_quantity("1/3"), 1)
        self.assertEqual(parse_quantity("2/3"), 1)
        self.assertEqual(parse_quantity("3/3"), 1)


if __name__ == "__main__":
    unittest.main()

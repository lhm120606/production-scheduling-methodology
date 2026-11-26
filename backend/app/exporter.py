from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .db import APP_BASE_DIR, get_conn, rows_to_dicts
from .rules import scheduler_config
from .scheduler import dashboard_data, full_schedule, stats_data


OUTPUT_DIR = APP_BASE_DIR / "outputs"
TASK_HEADERS = [
    "序号", "星期", "计划日期", "铸件型号", "铸件名称/件号", "铸件材质",
    "工序", "计划件数", "批号(随排产更新)", "单重(kg)", "本批总重(kg)",
    "计划吨位", "备注", "剩余订单(件)", "订单总数(件)", "交货期",
    "对应浇筑日期", "最晚完成日期", "优先级", "客户", "订单号", "合同号",
    "节点提醒", "任务性质", "风险状态", "排产原因", "是否锁定", "是否跨周",
    "预计工时(小时)",
]
WEEK_PLAN_HEADERS = [
    "序号", "铸件型号", "铸件名称/件号", "铸件材质", "本周计划数量",
    "本周工序", "批号(需要时)", "单重(kg)", "总重(kg)", "备注",
    "完成后剩余(件)", "交货期", "最晚节点",
]


def style_sheet(ws) -> None:
    header_fill = PatternFill("solid", fgColor="DCEAF7")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="17324D")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for column in ws.columns:
        values = [str(cell.value or "") for cell in column[:200]]
        width = min(max(max((len(value) for value in values), default=0) + 2, 10), 34)
        ws.column_dimensions[get_column_letter(column[0].column)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def write_rows(ws, headers: list[str], rows: list[list]) -> None:
    ws.append(headers)
    for row in rows:
        ws.append(row)
    style_sheet(ws)


def task_values(task: dict, sequence: int) -> list:
    task_date = datetime.strptime(task["date"], "%Y-%m-%d").date()
    return [
        sequence,
        f"星期{'一二三四五六日'[task_date.weekday()]}",
        task["date"],
        task.get("model_no"),
        task.get("display_product_name") or task.get("product_name"),
        task.get("material"),
        task["process_name"],
        task.get("planned_units"),
        task.get("production_batch_no"),
        task.get("unit_weight_kg"),
        task.get("total_weight_kg"),
        task.get("planned_weight_ton"),
        task.get("order_remark"),
        task.get("remaining_order_units"),
        task.get("piece_total"),
        task.get("due_date"),
        task.get("related_casting_date"),
        task.get("latest_finish_date") or task.get("mandatory_finish_at"),
        task["priority_level"],
        task.get("customer"),
        task["order_no"],
        task.get("contract_no"),
        task.get("deadline_alert_text"),
        task.get("task_nature"),
        task.get("risk_status"),
        task.get("decision_reason"),
        "是" if task.get("is_locked") else "否",
        "是" if task.get("is_cross_week") else "否",
        task.get("estimated_hours"),
    ]


def add_task_sheet(wb: Workbook, name: str, tasks: list[dict]) -> None:
    ws = wb.create_sheet(name)
    write_rows(ws, TASK_HEADERS, [task_values(task, index) for index, task in enumerate(tasks, start=1)])
    for cell in ws["H"][1:]:
        cell.number_format = "0"
    for cell in ws["J"][1:]:
        cell.number_format = "0.00"
    for cell in ws["K"][1:]:
        cell.number_format = "0.00"
    for cell in ws["L"][1:]:
        cell.number_format = "0.00"
    for cell in ws["AC"][1:]:
        cell.number_format = "0.00"
    warning_fill = PatternFill("solid", fgColor="FFF2CC")
    danger_fill = PatternFill("solid", fgColor="FCE8E6")
    for row_index, task in enumerate(tasks, start=2):
        if task.get("quantity_issue") or task.get("risk_status"):
            fill = danger_fill if task.get("quantity_issue") else warning_fill
            for cell in ws[row_index]:
                cell.fill = fill


def add_week_plan_sheet(wb: Workbook, data: dict) -> None:
    ws = wb.create_sheet("周生产计划")
    title = f"{data['iso_week'].replace('-W', '年第')}周生产计划（{data['week_start']} 至 {data['week_end']}）"
    ws.append([title])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(WEEK_PLAN_HEADERS))
    title_cell = ws.cell(1, 1)
    title_cell.font = Font(bold=True, size=18)
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30
    ws.append(WEEK_PLAN_HEADERS)
    for index, row in enumerate(data["week_plan_rows"], start=1):
        remarks = "；".join(
            text for text in (row.get("order_remark"), row.get("risk_status")) if text
        )
        ws.append(
            [
                index,
                row.get("model_no"),
                row.get("display_product_name") or row.get("product_name"),
                row.get("material"),
                row.get("planned_units"),
                row.get("process_name"),
                row.get("production_batch_no"),
                row.get("unit_weight_kg"),
                row.get("total_weight_kg"),
                remarks,
                row.get("remaining_order_units"),
                row.get("due_date"),
                row.get("latest_finish_date"),
            ]
        )
    header_fill = PatternFill("solid", fgColor="D9EAF7")
    for cell in ws[2]:
        cell.font = Font(bold=True, color="17324D")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in ws.iter_rows(min_row=3):
        for cell in row:
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    widths = [7, 20, 28, 16, 14, 14, 24, 12, 12, 28, 16, 14, 14]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(WEEK_PLAN_HEADERS))}{max(ws.max_row, 2)}"


def export_simple(week_start: date | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data = dashboard_data(week_start)
    wb = Workbook()
    wb.remove(wb.active)
    add_week_plan_sheet(wb, data)
    add_task_sheet(wb, "本周任务总表", data["week_tasks"])
    add_task_sheet(wb, "本周浇筑主计划", data["week_casting"])
    add_task_sheet(wb, "本周必须造型任务", data["week_molding"])
    add_task_sheet(wb, "下周浇筑前置任务", data["next_week_prep_molding"])
    add_task_sheet(wb, "下周任务预告", data["next_week_tasks"])
    path = OUTPUT_DIR / (
        f"国刚周任务计划简表_{data['iso_week'].replace('-', '')}_"
        f"{uuid4().hex[:8]}.xlsx"
    )
    wb.save(path)
    return path


def export_full(week_start: date | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data = full_schedule()
    dashboard = dashboard_data(week_start)
    week_tasks = dashboard["week_tasks"]
    later_tasks = (
        dashboard["week_grinding"]
        + dashboard["week_machining"]
        + dashboard["week_inspection"]
    )
    wb = Workbook()
    wb.remove(wb.active)
    add_week_plan_sheet(wb, dashboard)
    add_task_sheet(wb, "本周任务总表", week_tasks)
    add_task_sheet(wb, "本周浇筑主计划", dashboard["week_casting"])
    add_task_sheet(wb, "本周必须造型任务", dashboard["week_molding"])
    add_task_sheet(wb, "下周浇筑前置任务", dashboard["next_week_prep_molding"])
    add_task_sheet(wb, "本周开箱任务", dashboard["week_opening"])
    add_task_sheet(wb, "本周后段任务", later_tasks)
    add_task_sheet(
        wb,
        "按日期周计划",
        sorted(week_tasks, key=lambda task: (
            task["date"], task["process_name"], task["priority_level"],
            task["order_id"],
        )),
    )
    add_task_sheet(
        wb,
        "按订单周计划",
        sorted(week_tasks, key=lambda task: (
            task["order_no"], task["date"], task["process_name"],
        )),
    )
    add_task_sheet(
        wb,
        "按工序周计划",
        sorted(week_tasks, key=lambda task: (
            task["process_name"], task["date"], task["priority_level"],
        )),
    )

    ws = wb.create_sheet("未来4周负载")
    write_rows(
        ws,
        ["周编号", "周开始", "周结束", "浇筑件数", "浇筑吨位",
         "推荐产能吨", "平均负载率%", "任务数"],
        [
            [
                row["iso_week"], row["week_start"], row["week_end"],
                row["planned_units"], row["planned_weight_ton"],
                row["target_capacity_ton"], row["usage_rate"],
                row["task_count"],
            ]
            for row in dashboard["four_week_load"]
        ],
    )
    for cell in ws["E"][1:]:
        cell.number_format = "0.00"
    for cell in ws["F"][1:]:
        cell.number_format = "0.00"

    risk_rows = [
        task
        for task in week_tasks + dashboard["next_week_tasks"]
        if task.get("risk_status")
        or task.get("quantity_issue")
        or task.get("task_nature") in ("上周顺延任务", "待确认任务")
    ]
    add_task_sheet(wb, "周计划风险清单", risk_rows)

    ws = wb.create_sheet("数据异常清单")
    write_rows(
        ws,
        ["严重程度", "订单号", "异常代码", "说明", "状态"],
        [
            [row["severity"], row["order_no"], row["issue_code"], row["message"], row["status"]]
            for row in data["issues"]
        ],
    )
    delayed_tasks = [
        task
        for task in (
            week_tasks + dashboard["next_week_tasks"]
        )
        if (
            task.get("due_date") and task["date"] > task["due_date"]
        ) or task.get("task_nature") == "上周顺延任务"
    ]
    add_task_sheet(
        wb,
        "延期和顺延任务",
        delayed_tasks,
    )

    ws = wb.create_sheet("计划调整记录")
    write_rows(
        ws,
        ["订单号", "工序", "调整前日期", "调整后日期", "调整前件数",
         "调整后件数", "调整原因", "急单/插单触发", "操作时间"],
        [
            [
                row["order_no"], row["process_name"], row["before_date"],
                row["after_date"], row.get("before_units"),
                row.get("after_units"), row["reason"],
                "是" if row.get("triggered_by_urgent") else "否",
                row["created_at"],
            ]
            for row in dashboard["adjustments"]
        ],
    )

    with get_conn() as conn:
        config = scheduler_config(conn)
    ws = wb.create_sheet("系统规则摘要")
    rules = [
        ["算法核心", "先排浇筑主计划，再反推造型，随后安排开箱和后段"],
        ["周计划口径", "自然周周一至周日连续生产；首页只下达整周任务，不细分每日作业"],
        ["首页管理单位", "任意自然周任务计划、后一周任务预告、从所选周起未来4周负载"],
        ["本次导出周", f"{dashboard['week_relation']} {dashboard['week_start']} 至 {dashboard['week_end']}"],
        ["排产生产日", config["horizon_days"]],
        ["生产星期", ",".join(str(day + 1) for day in config["working_weekdays"])],
        ["浇筑推荐目标(吨)", config["casting_target_ton"]],
        ["浇筑正常下限(吨)", config["casting_normal_min_ton"]],
        ["浇筑正常上限(吨)", config["casting_normal_max_ton"]],
        ["浇筑绝对上限(吨)", config["casting_absolute_max_ton"]],
        ["造型提前生产日", config["molding_lead_days"]],
        ["QT推荐炉次(吨)", f"{config['qt_furnace_min_ton']}-{config['qt_furnace_max_ton']}"],
        ["开箱规则", "按开箱时间工作表查单件重量区间；QT400-QT500取下限，其余取上限"],
        ["计划显示口径", "计划件数为主，计划吨位=本批次件数×单件重量÷1000，开箱小时独立列示"],
        ["缺失数据处理", "不编造；进入异常订单表并等待人工确认"],
        ["现场执行边界", "系统下达周任务与最晚节点，班组在周内自行安排具体作业顺序"],
        ["锁定规则", "支持整周、日期、订单和浇筑批次；重排保留锁定浇筑任务"],
        ["本次排产版本", dashboard["schedule_version"]],
        ["最近排产时间", dashboard["last_run_time"]],
    ]
    write_rows(ws, ["规则项", "规则值"], rules)

    path = OUTPUT_DIR / (
        f"国刚周任务计划_{dashboard['iso_week'].replace('-', '')}_"
        f"{uuid4().hex[:8]}.xlsx"
    )
    wb.save(path)
    return path

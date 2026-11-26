from __future__ import annotations

import re
from datetime import date, timedelta

from .db import get_conn, json_dumps, now_iso, rows_to_dicts
from .scheduler import dashboard_data, full_schedule, risk_orders, stats_data


def get_ai_config() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key='ai_config'").fetchone()
        if not row:
            return {"enabled": False, "apiKey": "", "modelName": "", "baseUrl": ""}
        import json
        return json.loads(row["value"])


def save_ai_config(payload: dict) -> dict:
    safe_payload = {
        "enabled": bool(payload.get("enabled")),
        "apiKey": payload.get("apiKey") or "",
        "modelName": payload.get("modelName") or "",
        "baseUrl": payload.get("baseUrl") or "",
    }
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at) VALUES ('ai_config', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (json_dumps(safe_payload), now_iso()),
        )
    masked = dict(safe_payload)
    masked["apiKey"] = "已配置" if masked["apiKey"] else ""
    return masked


def answer_question(question: str) -> dict:
    q = question.strip()
    if not q:
        return {"answer": "请先输入要查询的问题。", "links": []}
    data = dashboard_data()
    links = []

    if any(word in q for word in ["今日任务总结", "今天", "今日"]) and any(word in q for word in ["总结", "多少", "任务", "浇筑", "打磨", "加工", "检验"]):
        lines = [f"今天（{data['today']}）任务："]
        for item in data["summary"]:
            lines.append(
                f"{item['process_name']} {item.get('planned_units', 0)}件"
                f"（{item.get('planned_weight_ton', 0):.2f}吨），"
                f"内部产能使用率 {item['usage_rate']}%。"
            )
        if data["risk_orders"]:
            top = data["risk_orders"][0]
            lines.append(f"最急订单：{top['order_no']}，优先级 {top['priority_level']}，交期 {top.get('due_date') or '未填'}。")
            links.append({"label": top["order_no"], "type": "order", "id": top["id"]})
        return {"answer": "\n".join(lines), "links": links}

    if "明天" in q or "明日" in q:
        tasks = data["tomorrow_tasks"]
        if not tasks:
            return {"answer": "当前排产里明天没有任务。", "links": []}
        lines = [f"明天（{data['tomorrow']}）预告："]
        for task in tasks[:20]:
            customer = task.get("customer") or "客户未填"
            due_date = task.get("due_date") or "未填"
            urgent = "，急单" if task.get("is_urgent") else ""
            rolled = "，滚动任务" if task.get("rolled_over") else ""
            units = (
                f"{task['planned_units']}件"
                if task.get("planned_units") is not None
                else "计划件数缺失"
            )
            ton = (
                f"（{float(task['planned_weight_ton']):.2f}吨）"
                if task.get("planned_weight_ton") is not None
                else ""
            )
            lines.append(
                f"{task['priority_level']}：{task['order_no']}，客户 {customer}，"
                f"{task['process_name']} {units}{ton}，"
                f"交期 {due_date}{urgent}{rolled}。"
            )
            links.append({"label": task["order_no"], "type": "order", "id": task["order_id"]})
        if len(tasks) > 20:
            lines.append(f"还有 {len(tasks) - 20} 条明日任务，请到“明日任务预告”表格或完整排产查看。")
        return {"answer": "\n".join(lines), "links": links}

    if "风险" in q or "快到期" in q or "最急" in q:
        risks = data["risk_orders"][:10]
        if not risks:
            return {"answer": "当前没有 P0/P1/P2 风险订单。", "links": []}
        lines = ["当前风险订单："]
        for item in risks:
            lines.append(f"{item['priority_level']}：{item['order_no']}，客户 {item.get('customer') or '-'}，交期 {item.get('due_date') or '未填'}，缓冲 {item['buffer_days']} 天。")
            links.append({"label": item["order_no"], "type": "order", "id": item["id"]})
        return {"answer": "\n".join(lines), "links": links}

    if "逾期" in q:
        overdue = data["overdue_orders"]
        if not overdue:
            return {"answer": "当前没有已逾期且未完成的订单。", "links": []}
        lines = ["已逾期订单："]
        for item in overdue[:10]:
            lines.append(f"{item['order_no']}，交期 {item.get('due_date')}，优先级 {item['priority_level']}。")
        return {"answer": "\n".join(lines), "links": []}

    order_no = extract_order_no(q)
    if order_no:
        return answer_order(order_no)

    if "瓶颈" in q or "产能够不够" in q or "加班" in q:
        schedule = full_schedule()
        bottlenecks = []
        for process in schedule["by_process"]:
            high = [day for day in process["days"] if day["usage_rate"] >= 95]
            if high:
                bottlenecks.append(f"{process['process_name']}：{len(high)} 天接近满负荷，最近一天 {high[0]['date']} 使用率 {high[0]['usage_rate']}%。")
        if not bottlenecks:
            return {"answer": "按当前排产，未来没有明显满负荷工序；是否需要加班要结合临时插单和当天实际完成情况判断。", "links": []}
        return {"answer": "当前瓶颈判断：\n" + "\n".join(bottlenecks), "links": []}

    if "日报" in q or "早会" in q:
        lines = ["生产日报/早会简版："]
        for item in data["summary"]:
            lines.append(f"- {item['process_name']}计划 {item['planned_qty']}{item['unit']}，产能使用率 {item['usage_rate']}%。")
        if data["risk_orders"]:
            lines.append(f"- 重点盯防 {len(data['risk_orders'])} 个风险订单，优先处理 P0/P1。")
        if data["overdue_orders"]:
            lines.append(f"- 已逾期订单 {len(data['overdue_orders'])} 个，需要确认原因和补救安排。")
        return {"answer": "\n".join(lines), "links": []}

    stats = stats_data()
    if "泡沫" in q or "抬模" in q or "木型" in q or "在手" in q:
        lines = [f"当前在手未完成订单 {stats['open_orders']} 个。"]
        for row in stats["by_product_type"]:
            lines.append(f"{row['product_type']}：{row['order_count']} 单，余件数约 {round(row['quantity'] or 0, 2)}，重量约 {round(row['weight_ton'] or 0, 2)} 吨。")
        return {"answer": "\n".join(lines), "links": []}

    return {"answer": "当前数据不足，无法判断。你可以问：今日任务总结、明日任务预告、查看风险订单、查看逾期订单、某个订单什么时候完成、查看产能瓶颈。", "links": []}


def extract_order_no(question: str) -> str | None:
    match = re.search(r"[A-Za-z0-9][A-Za-z0-9_-]{4,}", question)
    return match.group(0) if match else None


def answer_order(order_no: str) -> dict:
    schedule = full_schedule({"q": order_no})
    orders = [item for item in schedule["by_order"] if order_no.lower() in item["order_no"].lower()]
    if not orders:
        return {"answer": f"当前排产里没有找到订单 {order_no}。", "links": []}
    order = orders[0]
    lines = [
        f"订单 {order['order_no']} 预计完成日期：{order.get('estimated_finish_date') or '暂无'}。",
        f"优先级：{order['priority_level']}，交期：{order.get('due_date') or '未填'}。",
    ]
    if order["delay_days"] > 0:
        lines.append(f"预计延期 {order['delay_days']} 天，需要优先处理或增加相关工序产能。")
    else:
        lines.append("按当前排产预计可以赶上交期。")
    for task in order["tasks"][:8]:
        units = task.get("planned_units")
        plan = f"{units}件" if units is not None else "计划件数缺失"
        if task.get("planned_weight_ton") is not None:
            plan += f"（{float(task['planned_weight_ton']):.2f}吨）"
        if task.get("estimated_hours") is not None:
            plan += f"，预计工时{task['estimated_hours']}小时"
        lines.append(f"{task['date']}：{task['process_name']} {plan}。")
    return {"answer": "\n".join(lines), "links": [{"label": order["order_no"], "type": "order", "id": order["order_id"]}]}

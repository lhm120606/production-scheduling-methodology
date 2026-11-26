import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BellRing, Bot, CalendarDays, CheckSquare, ChevronDown, Download, Factory, History, Home, ListFilter, PackagePlus, RefreshCw, Settings, Table2 } from 'lucide-react';
import './styles.css';

const API = window.location.port === '5174' ? 'http://127.0.0.1:8000/api' : '/api';
const productTypes = ['泡沫实型', '抬模', '木型'];
const processes = ['木模造型', '消失模造型', '浇筑', '开箱', '打磨', '加工', '检验'];
const priorities = ['P0', 'P1', 'P2', 'P3'];

type AnyObj = Record<string, any>;

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, options);
  if (!res.ok) {
    let detail = '请求失败';
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function App() {
  const [page, setPage] = useState('dashboard');
  const [notice, setNotice] = useState('');
  const nav = [
    ['dashboard', Home, '周计划'],
    ['reminders', BellRing, '每日提醒'],
    ['full', CalendarDays, '完整排产'],
    ['orders', Table2, '订单管理'],
    ['new', PackagePlus, '新订单录入'],
    ['capacity', Settings, '产能配置'],
    ['checkin', CheckSquare, '每日打卡'],
    ['history', History, '排产历史'],
    ['stats', Factory, '统计汇总'],
  ] as const;
  return (
    <div className="app">
      <aside className="nav">
        <h1>国刚滚动排产</h1>
        <p>周计划 · 节点提醒 · 滚动调整</p>
        {nav.map(([key, Icon, label]) => (
          <button key={key} className={page === key ? 'active' : ''} onClick={() => setPage(key)}>
            <Icon size={18} /> {label}
          </button>
        ))}
      </aside>
      <main>
        {notice && <div className="app-notice">{notice}</div>}
        {page === 'dashboard' && <Dashboard goFull={() => setPage('full')} goReminders={() => setPage('reminders')} setNotice={setNotice} />}
        {page === 'reminders' && <DailyReminders setNotice={setNotice} />}
        {page === 'full' && <FullSchedule />}
        {page === 'orders' && <Orders />}
        {page === 'new' && <NewOrder setNotice={setNotice} />}
        {page === 'capacity' && <Capacity setNotice={setNotice} />}
        {page === 'checkin' && <Checkin setNotice={setNotice} />}
        {page === 'history' && <HistoryPage />}
        {page === 'stats' && <Stats />}
      </main>
      <Assistant />
    </div>
  );
}

function Dashboard({ goFull, goReminders, setNotice }: { goFull: () => void; goReminders: () => void; setNotice: (v: string) => void }) {
  const [data, setData] = useState<AnyObj | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [selectedWeek, setSelectedWeek] = useState('');
  const load = async (weekStart = selectedWeek) => {
    const path = weekStart ? `/schedule/week?week_start=${encodeURIComponent(weekStart)}` : '/schedule/week';
    const next = await api<AnyObj>(path);
    setData(next);
    setSelectedWeek(next.week_start);
    return next;
  };
  useEffect(() => { load('').catch(e => setNotice(e.message)).finally(() => setLoading(false)); }, []);
  const changeWeek = async (weekStart: string) => {
    if (!weekStart || weekStart === data?.week_start) return;
    setLoading(true);
    try {
      await load(weekStart);
    } catch (error: any) {
      setNotice(error.message || '切换周计划失败');
    } finally {
      setLoading(false);
    }
  };
  const rerun = async () => {
    if (running) return;
    setRunning(true);
    try {
      await api('/schedule/run', { method: 'POST' });
      await load(selectedWeek);
      setNotice('已按“浇筑主计划 → 反推造型 → 后段滚动”重新排产');
    } catch (error: any) {
      setNotice(error.message || '重新排产失败');
    } finally {
      setRunning(false);
    }
  };
  const lockScope = async (scope_type: string, scope_value: string, reason: string) => {
    try {
      const next = await api<AnyObj>('/schedule/scope-lock', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ scope_type, scope_value, process_name:'浇筑', reason }),
      });
      await load(selectedWeek);
      setNotice(`已锁定${reason}，重排后保留原浇筑任务；版本 ${next.schedule_version}`);
    } catch (error: any) {
      setNotice(error.message || '锁定失败');
    }
  };
  const unlockScope = async (scope_type: string, scope_value: string, reason: string) => {
    try {
      await api(`/schedule/scope-lock?scope_type=${encodeURIComponent(scope_type)}&scope_value=${encodeURIComponent(scope_value)}&process_name=${encodeURIComponent('浇筑')}`, { method:'DELETE' });
      await load(selectedWeek);
      setNotice(`已解除${reason}锁定，未锁定任务已重新排产`);
    } catch (error: any) {
      setNotice(error.message || '解锁失败');
    }
  };
  if (loading) return <Page title="周任务计划中心"><div className="loading-state">正在读取所选自然周计划...</div></Page>;
  if (!data) return <Page title="周任务计划中心"><Empty text="暂时无法读取周计划，请稍后重试" /></Page>;
  const summary = data.weekly_summary;
  const weekLocked = data.active_scope_locks?.some((row: AnyObj) => row.scope_type === 'week' && row.scope_value === data.week_start);
  const weekIndex = data.available_weeks.findIndex((row: AnyObj) => row.week_start === data.week_start);
  const previousWeek = weekIndex > 0 ? data.available_weeks[weekIndex - 1] : null;
  const nextWeek = weekIndex >= 0 && weekIndex < data.available_weeks.length - 1 ? data.available_weeks[weekIndex + 1] : null;
  return (
    <Page title="周任务计划中心" actions={<><button onClick={() => weekLocked ? unlockScope('week', data.week_start, data.week_relation) : lockScope('week', data.week_start, `${data.week_relation} ${data.week_start} 至 ${data.week_end}`)}>{weekLocked ? `解锁${data.week_relation}` : `锁定${data.week_relation}`}</button><button onClick={goFull}><ListFilter size={16}/> 查看完整排产</button><button onClick={() => exportFile(`/export/full?week_start=${encodeURIComponent(data.week_start)}`)}><Download size={16}/> 导出所选周</button><button disabled={running} onClick={rerun}><RefreshCw size={16} className={running ? 'spin' : ''}/> {running ? '正在重排' : '重新排产'}</button></>}>
      <section className="week-switcher">
        <button disabled={!previousWeek} onClick={() => previousWeek && changeWeek(previousWeek.week_start)}>← 上一周</button>
        <label><span>调取周计划</span><select value={data.week_start} onChange={e => changeWeek(e.target.value)}>{data.available_weeks.map((row: AnyObj) => <option value={row.week_start} key={row.week_start}>{row.relation} · {row.iso_week} · {row.week_start} 至 {row.week_end} · {row.task_count}项</option>)}</select></label>
        <button disabled={!nextWeek} onClick={() => nextWeek && changeWeek(nextWeek.week_start)}>下一周 →</button>
      </section>
      <div className="selected-week-banner"><strong>{data.week_relation}</strong><span>{data.iso_week} · {data.week_start} 至 {data.week_end}</span><small>当前查看的是完整自然周计划；班组可在周内自行协调具体作业顺序。</small></div>
      <div className="run-meta">最近排产 {data.last_run_time || '-'} · 版本 {data.schedule_version}</div>
      <section className="summary-grid primary-summary">
        <div className="metric primary"><span>{data.week_relation}浇筑</span><strong>{summary.casting_units}件</strong><small>{Number(summary.casting_weight_ton).toFixed(2)}吨 · 平均负载 {summary.average_casting_usage_rate}%</small></div>
        <div className="metric primary"><span>{data.week_relation}必须造型</span><strong>{summary.molding_units}件</strong><small>含为{data.next_week_relation}浇筑提前完成的任务</small></div>
        <div className="metric"><span>{data.week_relation}计划行数</span><strong>{summary.plan_row_count}</strong><small>同一订单、同一工序已按周合并</small></div>
        <div className={`metric ${data.week_deadline_alerts?.length ? 'warning-metric' : ''}`}><span>节点提醒</span><strong>{data.week_deadline_alerts?.length || 0}</strong><small>所选周需要重点关注</small></div>
      </section>
      <WeeklyPlanTable title={`${data.iso_week.replace('-W', '年第')}周生产计划`} rows={data.week_plan_rows || []} />
      <DeadlineAlertSummary alerts={data.week_deadline_alerts || []} onOpen={goReminders} />
    </Page>
  );
}

function DeadlineAlertSummary({ alerts, onOpen }: { alerts: AnyObj[]; onOpen: () => void }) {
  const keyAlerts = alerts.filter(row => ['overdue', 'urgent', 'warning'].includes(row.level)).slice(0, 5);
  return <section className="deadline-summary">
    <div className="deadline-summary-head"><div><BellRing size={19}/><strong>最晚节点提醒</strong><span>直接显示各班组必须完成的工作和最晚日期</span></div><button onClick={onOpen}>查看每日提醒</button></div>
    {keyAlerts.length === 0 ? <div className="deadline-clear">当前没有7天内的临期造型节点</div> :
      <div className="deadline-summary-list">{keyAlerts.map((row, index) =>
        <div className={`deadline-summary-item ${row.level}`} key={`${row.task_id || row.batch_key}-${index}`}>
          <b>{deadlineLabel(row)}</b><span>{row.message}</span>
        </div>
      )}</div>}
  </section>;
}

function DailyReminders({ setNotice }: { setNotice: (v: string) => void }) {
  const [data, setData] = useState<AnyObj | null>(null);
  const [loading, setLoading] = useState(true);
  const [groupFilter, setGroupFilter] = useState('');
  const [levelFilter, setLevelFilter] = useState('');
  const load = () => {
    setLoading(true);
    api<AnyObj>('/reminders/daily')
      .then(setData)
      .catch(error => setNotice(error.message || '读取每日提醒失败'))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, []);
  if (loading) return <Page title="每日节点提醒"><div className="loading-state">正在按今天的日期计算班组提醒...</div></Page>;
  if (!data) return <Page title="每日节点提醒"><Empty text="暂时无法读取提醒，请稍后重试" /></Page>;
  const visibleReminders = data.reminders.filter((row: AnyObj) =>
    (!groupFilter || row.group_name === groupFilter) && (!levelFilter || row.level === levelFilter)
  );
  return <Page title="每日节点提醒" actions={<button onClick={load}><RefreshCw size={16}/> 刷新今日提醒</button>}>
    <div className="run-meta">{data.today} · 每次打开或刷新都会重新计算 · 版本 {data.schedule_version}</div>
    <div className="reminder-rule-note">{data.rule_note}</div>
    <section className="summary-grid reminder-metrics">
      <div className="metric"><span>全部提醒</span><strong>{data.summary.total}</strong><small>未来7天最晚节点</small></div>
      <div className="metric reminder-overdue"><span>需要立即完成</span><strong>{data.summary.overdue}</strong><small>节点已到，优先处理</small></div>
      <div className="metric reminder-urgent"><span>今天/明天必须处理</span><strong>{data.summary.urgent}</strong><small>不可继续后移</small></div>
      <div className="metric reminder-warning"><span>即将临期</span><strong>{data.summary.warning}</strong><small>提前安排班组资源</small></div>
    </section>
    <div className="panel"><div className="panel-head"><div><h2>按班组查看</h2><small>点击班组，只看与该组有关的节点</small></div>{groupFilter && <button onClick={() => setGroupFilter('')}>查看全部班组</button>}</div><div className="group-reminder-grid">{data.group_summary.map((row: AnyObj) =>
      <button className={`group-reminder-card ${groupFilter === row.group_name ? 'active' : ''}`} onClick={() => setGroupFilter(groupFilter === row.group_name ? '' : row.group_name)} key={row.group_name}><strong>{row.group_name}</strong><b>{row.total}条</b><small>立即完成 {row.overdue} · 今天/明天 {row.urgent} · 即将到期 {row.warning}</small></button>
    )}</div></div>
    <div className="panel reminder-list-panel"><div className="panel-head"><div><h2>今天需要关注的节点</h2><small>{visibleReminders.length} 条符合当前筛选</small></div><div className="reminder-toolbar"><select aria-label="提醒级别" value={levelFilter} onChange={e => setLevelFilter(e.target.value)}><option value="">全部级别</option><option value="overdue">需要立即完成</option><option value="urgent">今天/明天完成</option><option value="warning">即将到期</option><option value="notice">后续任务</option></select></div></div>
      {visibleReminders.length === 0 ? <Empty text="当前筛选下没有需要提醒的节点" /> :
        <div className="daily-reminder-list">{visibleReminders.map((row: AnyObj, index: number) =>
          <article className={`daily-reminder ${row.level}`} key={`${row.task_id || row.batch_key}-${row.process_name}-${index}`}>
            <div className="daily-reminder-tag">{deadlineLabel(row)}</div>
            <div><h3>{row.group_name} · 订单 {row.order_no}</h3><p>{row.message}</p><small>最晚完成：{row.deadline_date} · 对应浇筑：{row.casting_date}</small></div>
          </article>
        )}</div>}
    </div>
  </Page>;
}

function CastingWeekTable({ title, rows, onLockDay }: { title: string; rows: AnyObj[]; onLockDay: (day: string) => void }) {
  return <div className="panel"><h2>{title}</h2><div className="week-day-grid">{rows.map(row => <div className={`week-day ${row.status === '正常' ? 'normal' : row.status === '休息日' ? 'rest' : 'risk'}`} key={row.date}><strong>周{row.weekday} · {row.date.slice(5)}</strong><span>{row.planned_units}件（{Number(row.planned_weight_ton).toFixed(2)}吨）</span><small>负载 {row.usage_rate}% · {row.status}</small>{row.is_production_day && row.tasks.length > 0 && <button onClick={() => onLockDay(row.date)}>锁定当天</button>}</div>)}</div></div>;
}

function WeeklyPlanTable({ title, rows }: { title: string; rows: AnyObj[] }) {
  const categoryDefinitions = [
    { key: 'molding', name: '造型任务', description: '木模造型、消失模造型', processes: ['木模造型', '消失模造型'] },
    { key: 'casting', name: '浇筑任务', description: '本周需要完成的浇筑', processes: ['浇筑'] },
    { key: 'opening', name: '开箱任务', description: '本周到达开箱节点的铸件', processes: ['开箱'] },
    { key: 'grinding', name: '打磨任务', description: '本周需要完成的清理打磨', processes: ['打磨'] },
    { key: 'finishing', name: '加工与检验', description: '加工、终检和交付前任务', processes: ['加工', '检验'] },
  ];
  const knownProcesses = new Set(categoryDefinitions.flatMap(category => category.processes));
  const categories = categoryDefinitions
    .map(category => ({ ...category, rows: rows.filter(row => category.processes.includes(row.process_name)) }))
    .concat([{ key: 'other', name: '其他任务', description: '尚未归入固定班组的任务', processes: [], rows: rows.filter(row => !knownProcesses.has(row.process_name)) }])
    .filter(category => category.rows.length > 0);
  const [openCategory, setOpenCategory] = useState('');
  useEffect(() => { setOpenCategory(''); }, [title]);

  return <div className="panel weekly-plan-panel">
    <div className="weekly-plan-title"><div><h2>{title}</h2><small>按交货期倒排每道工序；先选任务类别，再查看该班组本周可以完成的具体铸件</small></div><span>{rows.length}项周任务</span></div>
    {rows.length === 0 ? <Empty text="本周暂时没有需要下达的生产任务" /> : <div className="weekly-category-list">{categories.map(category => {
      const isOpen = openCategory === category.key;
      const units = category.rows.reduce((total, row) => total + (Number(row.planned_units) || 0), 0);
      const weight = category.rows.reduce((total, row) => total + (Number(row.planned_weight_ton) || 0), 0);
      const urgentCount = category.rows.filter(row => row.is_urgent || row.risk_status).length;
      return <section className={`weekly-category category-${category.key} ${isOpen ? 'open' : ''}`} key={category.key}>
        <button className="weekly-category-toggle" type="button" aria-expanded={isOpen} aria-controls={`weekly-category-${category.key}`} onClick={() => setOpenCategory(isOpen ? '' : category.key)}>
          <span className="weekly-category-name"><b>{category.name}</b><small>{category.description}</small></span>
          <span className="weekly-category-stats"><span><b>{category.rows.length}</b>项</span><span><b>{Math.round(units)}</b>件</span><span><b>{weight.toFixed(2)}</b>吨</span>{urgentCount > 0 && <span className="category-alert"><b>{urgentCount}</b>项需关注</span>}</span>
          <span className="weekly-category-action">{isOpen ? '收起明细' : '查看本周任务'}<ChevronDown size={18}/></span>
        </button>
        {isOpen && <div className="weekly-category-detail" id={`weekly-category-${category.key}`}><WeeklyPlanRows rows={category.rows}/></div>}
      </section>;
    })}</div>}
  </div>;
}

function WeeklyPlanRows({ rows }: { rows: AnyObj[] }) {
  return <table className="weekly-plan-table"><thead><tr><th>序号</th><th>铸件型号</th><th>铸件名称 / 件号</th><th>铸件材质</th><th>本周计划数量</th><th>本周工序</th><th>批号<br/><small>需要时</small></th><th>单重<br/>(kg)</th><th>总重<br/>(kg)</th><th>备注</th><th>完成后剩余</th><th>交货期</th><th>最晚节点</th></tr></thead><tbody>{rows.map((r, index) => <tr className={r.is_urgent || r.risk_status ? 'attention-row' : ''} key={`${r.order_id}-${r.process_name}`}><td className="row-number">{index + 1}</td><td><strong>{r.model_no || r.order_no}</strong>{r.contract_no ? <small>合同 {r.contract_no}</small> : null}</td><td><strong>{r.product_name || r.product_type || '-'}</strong><span className="piece-label">{r.piece_label || '件号待确认'}</span><small>{r.customer || '-'}</small></td><td>{r.material || <span className="missing-plan">待补</span>}</td><td><strong>{r.planned_units != null ? `${Math.round(Number(r.planned_units))}件` : '待确认'}</strong><small>{r.planned_weight_ton != null ? `${Number(r.planned_weight_ton).toFixed(2)}吨` : '吨位待补'}</small></td><td><span className={`process-tag process-${r.process_name}`}>{r.process_name}</span></td><td><span className="batch-no">{r.production_batch_no || '待排'}</span></td><td>{r.unit_weight_kg != null ? fmt(r.unit_weight_kg) : <span className="missing-plan">待补</span>}</td><td>{r.total_weight_kg != null ? fmt(r.total_weight_kg) : '-'}</td><td>{r.order_remark || r.risk_status || '-'}{r.is_urgent ? <span className="urgent-mark">急单</span> : null}</td><td><strong>{fmt(r.remaining_order_units)}件</strong><small>订单共{fmt(r.piece_total)}件</small></td><td>{r.due_date || '-'}</td><td><strong>{r.latest_finish_date || '-'}</strong>{r.risk_status ? <small className="task-risk">{r.risk_status}</small> : null}</td></tr>)}</tbody></table>;
}

function FourWeekLoad({ title, rows }: { title: string; rows: AnyObj[] }) {
  return <div className="panel"><h2>{title}</h2><div className="four-week-grid">{rows.map(row => <div className="week-load-card" key={row.week_start}><strong>{row.iso_week}</strong><span>{row.week_start} 至 {row.week_end}</span><b>{row.planned_units}件（{Number(row.planned_weight_ton).toFixed(2)}吨）</b><small>推荐产能 {Number(row.target_capacity_ton).toFixed(2)}吨 · 负载 {row.usage_rate}% · {row.task_count}项任务</small></div>)}</div></div>;
}

function ShopFloorPlanTable({ title, rows, emptyText = '暂无任务', onLockBatch, onLockOrder }: { title: string; rows: AnyObj[]; emptyText?: string; onLockBatch?: (batch: string) => void; onLockOrder?: (orderId: number) => void }) {
  return <div className="panel shop-floor-panel"><div className="panel-head"><div><h2>{title}</h2><small>按员工排产表口径展示；名称后的件号区间用于区分大订单分批任务</small></div><span className="task-count">{rows.length}项</span></div>{rows.length === 0 ? <Empty text={emptyText} /> : <table className="shop-floor-table"><thead><tr><th>序号</th><th>计划日期</th><th>铸件型号</th><th>铸件名称 / 件号</th><th>铸件材质</th><th>工序</th><th>计划数量</th><th>批号<br/><small>随排产更新</small></th><th>单重<br/>(kg)</th><th>本批总重<br/>(kg)</th><th>备注</th><th>剩余订单</th><th>交货期</th><th>最晚节点</th><th>状态 / 风险</th><th>锁定</th></tr></thead><tbody>{rows.map((r, index) => <tr className={r.is_urgent || r.risk_status ? 'attention-row' : ''} key={r.id || `${r.batch_key}-${index}`}><td className="row-number">{index + 1}</td><td><strong>{weekday(r.date)}</strong><small>{r.date}</small>{r.related_casting_date && r.process_name !== '浇筑' ? <small>浇筑 {r.related_casting_date}</small> : null}</td><td><strong>{r.model_no || r.order_no}</strong><small>订单 {r.order_no}</small>{r.contract_no ? <small>合同 {r.contract_no}</small> : null}</td><td><strong>{r.product_name || r.product_type || '-'}</strong><span className="piece-label">{r.piece_label || '件号待确认'}</span><small>{r.customer || '-'}</small></td><td>{r.material || <span className="missing-plan">待补</span>}</td><td>{r.process_name}</td><td><strong>{r.planned_units != null ? `${Math.round(Number(r.planned_units))}件` : '待确认'}</strong><small>{r.planned_weight_ton != null ? `${Number(r.planned_weight_ton).toFixed(2)}吨` : '吨位待补'}</small></td><td><span className="batch-no">{r.production_batch_no || '待排'}</span></td><td>{r.unit_weight_kg != null ? fmt(r.unit_weight_kg) : <span className="missing-plan">待补</span>}</td><td>{r.total_weight_kg != null ? fmt(r.total_weight_kg) : '-'}</td><td>{r.order_remark || r.risk_status || '-'}{r.is_urgent ? <span className="urgent-mark">急单</span> : null}</td><td><strong>{fmt(r.remaining_order_units)}件</strong><small>订单共{fmt(r.piece_total)}件</small></td><td>{r.due_date || '-'}</td><td>{r.latest_finish_date || r.mandatory_finish_at || '-'}<DeadlineCell task={r}/></td><td><Badge v={r.priority_level}/><span className="nature">{r.task_nature || '正常任务'}</span>{r.risk_status && <small className="task-risk">{r.risk_status}</small>}<small>{r.decision_reason || '-'}</small></td><td>{r.is_locked ? '已锁定' : <div className="lock-actions">{onLockOrder && <button onClick={() => onLockOrder(Number(r.order_id))}>订单</button>}{onLockBatch && r.batch_key && <button onClick={() => onLockBatch(r.batch_key)}>批次</button>}{!onLockOrder && !onLockBatch && '未锁定'}</div>}</td></tr>)}</tbody></table>}</div>;
}

function TaskTable({ title, rows, emptyText = '暂无任务', onLockBatch, onLockOrder }: { title: string; rows: AnyObj[]; emptyText?: string; onLockBatch?: (batch: string) => void; onLockOrder?: (orderId: number) => void }) {
  return <div className="panel"><div className="panel-head"><h2>{title}</h2><span className="task-count">{rows.length}项</span></div>{rows.length === 0 ? <Empty text={emptyText} /> : <table className="task-table"><thead><tr><th>星期/日期</th><th>优先级</th><th>订单/合同</th><th>客户/产品</th><th>材质</th><th>工序</th><th>计划</th><th>预计工时</th><th>对应浇筑</th><th>最晚完成</th><th>节点提醒</th><th>任务性质</th><th>风险与排产原因</th><th>锁定</th></tr></thead><tbody>{rows.map(r => <tr key={r.id || r.batch_key}><td>{weekday(r.date)}<small>{r.date}</small></td><td><Badge v={r.priority_level}/></td><td>{r.order_no}<small>{r.contract_no || '-'}</small></td><td>{r.customer || '-'}<small>{r.product_name || r.product_type || '-'}</small></td><td>{r.material || '-'}</td><td>{r.process_name}</td><td><PlanValue task={r}/></td><td>{r.estimated_hours != null ? `${Number(r.estimated_hours).toFixed(2)}小时` : '-'}</td><td>{r.related_casting_date || '-'}</td><td>{r.latest_finish_date || r.mandatory_finish_at || r.due_date || '-'}</td><td><DeadlineCell task={r}/></td><td><span className="nature">{r.task_nature || '本周正常任务'}</span>{r.is_cross_week ? <small>跨周</small> : null}</td><td>{r.risk_status && <span className="task-risk">{r.risk_status}</span>}{r.quantity_issue && <span className="task-risk">{r.quantity_issue}</span>}<small>{r.decision_reason || '-'}</small></td><td>{r.is_locked ? '已锁定' : <div className="lock-actions">{onLockOrder && <button onClick={() => onLockOrder(Number(r.order_id))}>锁定订单</button>}{onLockBatch && r.batch_key && <button onClick={() => onLockBatch(r.batch_key)}>锁定批次</button>}{!onLockOrder && !onLockBatch && '未锁定'}</div>}</td></tr>)}</tbody></table>}</div>;
}

function DeadlineCell({ task }: { task: AnyObj }) {
  if (!task.deadline_alert_text) return <span className="deadline-none">当前无临期提醒</span>;
  return <span className={`deadline-cell ${task.deadline_alert_level || 'notice'}`}><b>{deadlineLabel(task)}</b><small>{task.deadline_alert_text}</small></span>;
}

function PlanValue({ task }: { task: AnyObj }) {
  if (task.planned_units == null) return <span className="missing-plan">计划件数缺失</span>;
  const units = Math.round(Number(task.planned_units));
  if (task.planned_weight_ton == null) return <span className="plan-value" title="缺少单件重量，无法计算吨位">{units}件<small>缺少单件重量</small></span>;
  return <span className="plan-value">{units}件（{Number(task.planned_weight_ton).toFixed(2)}吨）</span>;
}

function IssueTable({ rows }: { rows: AnyObj[] }) {
  return <div className="panel issue-panel"><h2>数据缺失与人工确认</h2>{rows.length === 0 ? <Empty text="当前没有待确认异常" /> : <table><thead><tr><th>程度</th><th>订单号</th><th>异常</th><th>说明</th></tr></thead><tbody>{rows.slice(0, 30).map(row => <tr key={row.id} className={row.severity === '阻断' ? 'danger-row' : ''}><td>{row.severity}</td><td>{row.order_no || '-'}</td><td>{row.issue_code}</td><td>{row.message}</td></tr>)}</tbody></table>}</div>;
}

function Risk({ title, rows }: { title: string; rows: AnyObj[]; overdue?: boolean }) {
  return <div className="panel"><h2>{title}</h2>{rows.length === 0 ? <Empty text="暂无" /> : <table><thead><tr><th>级别</th><th>订单号</th><th>客户</th><th>交期</th><th>缓冲</th></tr></thead><tbody>{rows.map(r => <tr key={r.id} className={r.is_overdue ? 'danger-row' : ''}><td><Badge v={r.priority_level}/></td><td>{r.order_no}</td><td>{r.customer}</td><td>{r.due_date || '-'}</td><td>{fmt(r.buffer_days)}天</td></tr>)}</tbody></table>}</div>;
}

function FullSchedule() {
  const [view, setView] = useState('week');
  const [filters, setFilters] = useState<AnyObj>({});
  const [data, setData] = useState<AnyObj | null>(null);
  const qs = new URLSearchParams(Object.entries(filters).filter(([,v]) => v !== '' && v != null).map(([k,v]) => [k, String(v)])).toString();
  useEffect(() => { api<AnyObj>(`/schedule/full?${qs}`).then(setData).catch(console.error); }, [qs]);
  return <Page title="完整排产" actions={<button onClick={() => exportFile('/export/full')}><Download size={16}/> 导出完整排产</button>}>
    <Filters filters={filters} setFilters={setFilters} />
    <div className="tabs">{[['week','按周'],['date','按日期'],['order','按订单'],['process','按工序'],['gantt','时间线']].map(([k,l]) => <button className={view===k?'active':''} onClick={() => setView(k)} key={k}>{l}</button>)}</div>
    {!data ? <p>正在读取...</p> : view === 'week' ? <ByWeek data={data.by_week}/> : view === 'date' ? <ByDate data={data.by_date}/> : view === 'order' ? <ByOrder data={data.by_order}/> : view === 'process' ? <ByProcess data={data.by_process}/> : <Gantt data={data.gantt}/>}
  </Page>;
}

function Filters({ filters, setFilters }: { filters: AnyObj; setFilters: (v: AnyObj) => void }) {
  const set = (k: string, v: any) => setFilters({ ...filters, [k]: v });
  return <div className="filters">
    <input placeholder="订单号/客户搜索" value={filters.q || ''} onChange={e => set('q', e.target.value)} />
    <Select label="产品" value={filters.product_type || ''} items={productTypes} onChange={v => set('product_type', v)} />
    <Select label="优先级" value={filters.priority || ''} items={priorities} onChange={v => set('priority', v)} />
    <Select label="工序" value={filters.process || ''} items={processes} onChange={v => set('process', v)} />
    <input type="date" value={filters.date_from || ''} onChange={e => set('date_from', e.target.value)} />
    <input type="date" value={filters.date_to || ''} onChange={e => set('date_to', e.target.value)} />
    <label><input type="checkbox" checked={!!filters.overdue} onChange={e => set('overdue', e.target.checked)} /> 只看逾期</label>
    <label><input type="checkbox" checked={!!filters.urgent} onChange={e => set('urgent', e.target.checked)} /> 只看急单</label>
  </div>;
}

function ByWeek({ data }: { data: AnyObj[] }) {
  return <div className="stack">{data.map(week => <div className="panel" key={week.week_start}><h2>{week.iso_week} · {week.week_start} 至 {week.week_end}</h2><TaskTable title="周任务明细" rows={week.tasks}/></div>)}</div>;
}

function ByDate({ data }: { data: AnyObj[] }) {
  return <div className="stack">{data.map(day => <div className="panel" key={day.date}><h2>{day.date}</h2><div className="mini-metrics">{day.summary.filter((s: AnyObj) => s.planned_units > 0).map((s: AnyObj) => <span key={s.process_name}>{s.process_name} {s.planned_units}件（{Number(s.planned_weight_ton).toFixed(2)}吨）</span>)}</div><TaskTable title="任务" rows={day.tasks}/></div>)}</div>;
}

function ByOrder({ data }: { data: AnyObj[] }) {
  return <div className="stack">{data.map(o => <div className={`panel ${o.delay_days > 0 ? 'risk-panel' : ''}`} key={o.order_id}><h2><Badge v={o.priority_level}/> {o.order_no} · {o.customer || '-'}</h2><p>交期 {o.due_date || '-'}，预计完成 {o.estimated_finish_date || '-'}，{o.delay_days > 0 ? `预计延期 ${o.delay_days} 天` : '预计按期'}</p><TaskTable title="工序安排" rows={o.tasks}/></div>)}</div>;
}

function ByProcess({ data }: { data: AnyObj[] }) {
  return <section className="split wide">{data.map(p => <div className="panel" key={p.process_name}><h2>{p.process_name}</h2><table><thead><tr><th>日期</th><th>计划</th><th>内部产能</th><th>使用率</th><th>空余</th></tr></thead><tbody>{p.days.map((d: AnyObj) => <tr key={d.date} className={d.overloaded ? 'danger-row' : ''}><td>{d.date}</td><td>{Math.round(d.planned_units)}件（{Number(d.planned_weight_ton).toFixed(2)}吨）</td><td>{fmt(d.capacity)}{d.unit}</td><td>{d.usage_rate}%</td><td>{fmt(d.free_capacity)}{d.unit}</td></tr>)}</tbody></table></div>)}</section>;
}

function Gantt({ data }: { data: AnyObj[] }) {
  const dates = Array.from(new Set(data.flatMap(o => o.tasks.map((t: AnyObj) => t.date)))).sort().slice(0, 45);
  return <div className="panel gantt"><h2>简单时间线</h2>{data.map(o => <div className="gantt-row" key={o.order_id}><div className="gantt-label"><Badge v={o.priority_level}/> {o.order_no}</div><div className="gantt-line" style={{gridTemplateColumns:`repeat(${dates.length}, minmax(28px, 1fr))`}}>{dates.map(d => { const tasks = o.tasks.filter((t: AnyObj) => t.date === d); return <div className="day" title={d} key={d}>{tasks.map((t: AnyObj) => <span className={`block p-${t.process_name}`} key={t.id}>{t.process_name[0]}</span>)}</div>; })}</div></div>)}</div>;
}

function Orders() {
  const [rows, setRows] = useState<AnyObj[]>([]);
  const [status, setStatus] = useState('');
  const [product, setProduct] = useState('');
  useEffect(() => { api<AnyObj[]>(`/orders?status=${status}&product_type=${product}`).then(setRows); }, [status, product]);
  const updateStatus = async (id: number, next: string) => { await api(`/orders/${id}/status?status=${encodeURIComponent(next)}`, { method: 'POST' }); setRows(await api('/orders')); };
  return <Page title="订单管理"><div className="filters"><Select label="状态" value={status} items={['未开始','进行中','已完成','暂停','延期']} onChange={setStatus}/><Select label="产品" value={product} items={productTypes} onChange={setProduct}/></div><div className="panel"><table><thead><tr><th>订单号</th><th>客户</th><th>产品</th><th>件数</th><th>重量吨</th><th>交期</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows.map(r => <tr key={r.id}><td>{r.order_no}</td><td>{r.customer}</td><td>{r.product_type}</td><td>{fmt(r.quantity)}</td><td>{fmt(r.weight_ton)}</td><td>{r.due_date || '-'}</td><td>{r.status}</td><td><button onClick={() => updateStatus(r.id, r.status === '暂停' ? '进行中' : '暂停')}>{r.status === '暂停' ? '恢复' : '暂停'}</button></td></tr>)}</tbody></table></div></Page>;
}

function NewOrder({ setNotice }: { setNotice: (v: string) => void }) {
  const empty = { order_no: '', model_no: '', customer: '', product_name: '', material: '', product_type: '木型', molding_method: '木型', quantity: 0, weight_ton: 0, due_date: '', is_urgent: false, remark: '' };
  const [form, setForm] = useState<AnyObj>(empty);
  const [preview, setPreview] = useState<AnyObj | null>(null);
  const [mapping, setMapping] = useState<AnyObj>({});
  const [analysis, setAnalysis] = useState<AnyObj | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const validate = () => {
    if (!form.order_no.trim()) return '请填写订单号';
    if (!form.model_no.trim()) return '请填写铸件型号';
    if (!form.product_name.trim()) return '请填写铸件名称';
    if (Number(form.quantity) <= 0) return '件数必须大于0';
    if (Number(form.weight_ton) <= 0) return '订单总重量必须大于0';
    if (!form.due_date) return '请选择交期';
    return '';
  };
  const submit = async () => {
    const error = validate();
    if (error) return setNotice(error);
    setSubmitting(true);
    try {
      await api('/orders', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(cleanOrderPayload(form)) });
      setNotice('新订单已保存，周计划和每日提醒已重新计算');
      setForm(empty);
      setAnalysis(null);
    } finally {
      setSubmitting(false);
    }
  };
  const analyze = async () => {
    const error = validate();
    if (error) return setNotice(error);
    setAnalysis(await api('/orders/analyze', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(cleanOrderPayload(form)) }));
  };
  const upload = async (file: File) => { const fd = new FormData(); fd.append('file', file); const res = await fetch(`${API}/import/preview`, { method:'POST', body: fd }); const json = await res.json(); if (!res.ok) throw new Error(json.detail); setPreview(json); setMapping(json.suggested_mapping); };
  const commit = async (replaceExisting = false) => {
    if (replaceExisting && !window.confirm('将清空当前全部订单和旧排产，再用这份Excel重新排产。确认继续吗？')) return;
    const result = await api<AnyObj>('/import/commit', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ file_id: preview?.file_id, mapping, replace_existing: replaceExisting }) });
    setNotice(replaceExisting ? `已替换${result.import?.removed_orders || 0}条旧订单，导入${result.import?.inserted || 0}条新订单并重新排产` : 'Excel 已导入，重复订单已更新，并已重新排产');
    setPreview(null);
  };
  return <Page title="新订单录入"><div className="page-intro"><strong>录入后自动更新</strong><span>周计划、浇筑主计划、最晚节点和每日提醒会同步重算。</span></div><section className="split order-entry-grid"><div className="panel form-panel"><div className="panel-head"><div><h2>手动新增</h2><small>适合单个订单快速录入</small></div></div><OrderForm form={form} setForm={setForm}/><div className="actions"><button onClick={analyze}>先分析产能影响</button><button className="primary-action" disabled={submitting} onClick={submit}>{submitting ? '正在保存' : '保存并重新排产'}</button></div>{analysis && <div className="analysis"><strong>{analysis.risk}</strong><p>预计工作量 {analysis.estimated_work_days} 天，距交期 {analysis.days_to_due ?? '-'} 天，缓冲 {analysis.buffer_days ?? '-'} 天。</p><small>{analysis.note}</small></div>}</div><div className="panel import-panel"><div className="panel-head"><div><h2>Excel 批量导入</h2><small>适合一次更新多个订单</small></div></div><label className="file-drop"><span>选择 .xlsx 或 .xlsm 订单表</span><input type="file" accept=".xlsx,.xlsm" onChange={e => e.target.files?.[0] && upload(e.target.files[0]).catch(err => setNotice(err.message))}/></label>{preview && <div className="import-preview"><p>Sheet：{preview.sheet_name} · 表头行：{preview.header_row}</p><Mapping preview={preview} mapping={mapping} setMapping={setMapping}/><div className="import-actions"><button onClick={() => commit(false)}>追加或更新订单</button><button className="replace-action" onClick={() => commit(true)}>替换全部旧订单并重新排产</button></div><small className="replace-hint">替换模式会先校验有效订单，再清空旧订单和旧排产。</small></div>}</div></section></Page>;
}

function Mapping({ preview, mapping, setMapping }: { preview: AnyObj; mapping: AnyObj; setMapping: (v: AnyObj) => void }) {
  const fields = [['order_no','订单号'],['model_no','铸件型号'],['customer','客户'],['product_name','铸件名称'],['material','材质'],['product_type','产品类型'],['molding_method','造型方式'],['quantity','件数'],['weight_kg','单重KG'],['weight_ton','总重量吨'],['due_date','交期'],['remark','备注'],['is_urgent','急单']];
  return <div className="mapping">{fields.map(([key,label]) => <label key={key}>{label}<select value={mapping[key] || ''} onChange={e => setMapping({...mapping, [key]: e.target.value})}><option value="">不映射</option>{preview.headers.map((h: string) => <option key={h} value={h}>{h}</option>)}</select></label>)}</div>;
}

function OrderForm({ form, setForm }: { form: AnyObj; setForm: (v: AnyObj) => void }) {
  const set = (k: string, v: any) => setForm({ ...form, [k]: v });
  return <div className="form-grid labeled-form">
    <label><span>订单号 *</span><input placeholder="例如 GG-2026-001" value={form.order_no} onChange={e => set('order_no', e.target.value)}/></label>
    <label><span>铸件型号 *</span><input placeholder="请输入铸件型号" value={form.model_no} onChange={e => set('model_no', e.target.value)}/></label>
    <label><span>客户</span><input placeholder="客户名称" value={form.customer} onChange={e => set('customer', e.target.value)}/></label>
    <label><span>铸件名称 *</span><input placeholder="产品或铸件名称" value={form.product_name} onChange={e => set('product_name', e.target.value)}/></label>
    <label><span>材质</span><input placeholder="例如 HT300 / QT500-7" value={form.material} onChange={e => set('material', e.target.value)}/></label>
    <label><span>产品类型 *</span><select value={form.product_type} onChange={e => setForm({...form, product_type:e.target.value, molding_method:e.target.value})}>{productTypes.map(p => <option key={p}>{p}</option>)}</select></label>
    <label><span>订单件数 *</span><input type="number" min="1" step="1" placeholder="件" value={form.quantity || ''} onChange={e => set('quantity', Math.max(0, Math.round(Number(e.target.value))))}/></label>
    <label><span>订单总重量（吨）*</span><input type="number" min="0.01" step="0.01" placeholder="吨" value={form.weight_ton || ''} onChange={e => set('weight_ton', Number(e.target.value))}/></label>
    <label><span>交期 *</span><input type="date" value={form.due_date || ''} onChange={e => set('due_date', e.target.value)}/></label>
    <label className="urgent-check"><input type="checkbox" checked={form.is_urgent} onChange={e => set('is_urgent', e.target.checked)}/><span>标记为急单</span></label>
    <label className="full-field"><span>备注</span><textarea placeholder="补充工艺、客户或交期说明" value={form.remark} onChange={e => set('remark', e.target.value)}/></label>
  </div>;
}

function Capacity({ setNotice }: { setNotice: (v: string) => void }) {
  const [data, setData] = useState<AnyObj>({ defaults: [], overrides: [], history: [] });
  const [override, setOverride] = useState({ date: '', process_name: '浇筑', capacity: 0, unit: '吨', reason: '' });
  const load = () => api<AnyObj>('/capacity').then(setData);
  useEffect(() => { load(); }, []);
  const saveDefault = async (row: AnyObj) => { await api('/capacity/default', { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(row) }); setNotice('默认产能已保存并重新排产'); load(); };
  const saveOverride = async () => { await api('/capacity/override', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(override) }); setNotice('临时产能已保存并重新排产'); load(); };
  return <Page title="产能配置"><section className="split"><div className="panel"><h2>默认日产能</h2>{data.defaults.map((r: AnyObj) => <CapacityRow key={r.id} row={r} onSave={saveDefault}/>)}</div><div className="panel"><h2>临时产能</h2><div className="form-grid"><input type="date" value={override.date} onChange={e => setOverride({...override, date:e.target.value})}/><select value={override.process_name} onChange={e => setOverride({...override, process_name:e.target.value, unit:defaultUnit(e.target.value)})}>{processes.map(p => <option key={p}>{p}</option>)}</select><input type="number" value={override.capacity} onChange={e => setOverride({...override, capacity:Number(e.target.value)})}/><input value={override.unit} onChange={e => setOverride({...override, unit:e.target.value})}/><input placeholder="原因" value={override.reason} onChange={e => setOverride({...override, reason:e.target.value})}/></div><button onClick={saveOverride}>保存临时产能</button></div></section></Page>;
}

function CapacityRow({ row, onSave }: { row: AnyObj; onSave: (r: AnyObj) => void }) {
  const [local, setLocal] = useState(row);
  return <div className="capacity-row"><strong>{row.process_name}</strong><input type="number" value={local.daily_capacity} onChange={e => setLocal({...local, daily_capacity:Number(e.target.value)})}/><input value={local.unit} onChange={e => setLocal({...local, unit:e.target.value})}/><button onClick={() => onSave(local)}>保存</button></div>;
}

function Checkin({ setNotice }: { setNotice: (v: string) => void }) {
  const [data, setData] = useState<AnyObj | null>(null);
  const [items, setItems] = useState<AnyObj[]>([]);
  useEffect(() => { api<AnyObj>('/dashboard').then(d => { setData(d); setItems(d.today_tasks.map((t: AnyObj) => ({ task_id:t.id, order_id:t.order_id, process_name:t.process_name, planned_qty:t.planned_qty, actual_qty:t.planned_qty, planned_units:t.planned_units, actual_units:t.planned_units ?? 0, status:'完成', reason:'', remark:'' }))); }); }, []);
  const setItem = (i: number, patch: AnyObj) => setItems(items.map((it, idx) => idx===i ? {...it, ...patch} : it));
  const submit = async () => {
    const normalized = items.map(item => ({...item, actual_qty:item.planned_units ? Number(item.planned_qty) * Number(item.actual_units || 0) / Number(item.planned_units) : 0}));
    const result = await api<AnyObj>('/checkins', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ date:data?.today, idempotency_key:crypto.randomUUID(), items:normalized }) });
    setNotice(`打卡已保存 ${result.checkin?.accepted ?? normalized.length} 条，并按剩余件数重新排产`);
  };
  return <Page title="每日打卡">{!data ? <p>读取中...</p> : <div className="panel"><table><thead><tr><th>订单</th><th>工序</th><th>计划</th><th>实际完成件数</th><th>状态</th><th>未完成原因</th></tr></thead><tbody>{data.today_tasks.map((t: AnyObj, i: number) => <tr key={t.id}><td>{t.order_no}</td><td>{t.process_name}</td><td><PlanValue task={t}/></td><td><input type="number" min="0" step="1" max={t.planned_units ?? undefined} disabled={t.planned_units == null} value={items[i]?.actual_units ?? 0} onChange={e => setItem(i,{actual_units:Math.max(0, Math.round(Number(e.target.value)))})}/></td><td><select value={items[i]?.status} onChange={e => setItem(i,{status:e.target.value})}><option>完成</option><option>部分完成</option><option>未完成</option></select></td><td><input value={items[i]?.reason || ''} onChange={e => setItem(i,{reason:e.target.value})}/></td></tr>)}</tbody></table><button onClick={submit}>提交打卡并重新排产</button></div>}</Page>;
}

function HistoryPage() {
  const [rows, setRows] = useState<AnyObj[]>([]);
  useEffect(() => { api<AnyObj[]>('/schedule/history').then(setRows); }, []);
  return <Page title="排产历史"><div className="panel"><table><thead><tr><th>版本</th><th>时间</th><th>触发</th><th>摘要</th></tr></thead><tbody>{rows.map(r => <tr key={r.id}><td>{r.schedule_version}</td><td>{r.run_time}</td><td>{r.trigger_type}</td><td>{r.summary}</td></tr>)}</tbody></table></div></Page>;
}

function Stats() {
  const [data, setData] = useState<AnyObj | null>(null);
  useEffect(() => { api<AnyObj>('/stats').then(setData); }, []);
  if (!data) return <Page title="统计汇总"><p>读取中...</p></Page>;
  return <Page title="统计汇总"><section className="summary-grid"><div className="metric"><span>在手订单</span><strong>{data.open_orders}</strong><small>{data.can_finish_on_time ? '当前无 P0 风险' : '存在 P0 风险'}</small></div>{Object.entries(data.risk_counts).map(([k,v]) => <div className="metric" key={k}><span>{k}</span><strong>{String(v)}</strong><small>风险订单</small></div>)}</section><section className="split"><div className="panel"><h2>产品类型余量</h2><table><tbody>{data.by_product_type.map((r: AnyObj) => <tr key={r.product_type}><td>{r.product_type}</td><td>{r.order_count} 单</td><td>{fmt(r.quantity)} 件</td><td>{fmt(r.weight_ton)} 吨</td></tr>)}</tbody></table></div><div className="panel"><h2>工序余量</h2><table><tbody>{data.by_process.map((r: AnyObj) => <tr key={r.process_name}><td>{r.process_name}</td><td>{fmt(r.remaining_qty)} {r.unit}</td></tr>)}</tbody></table></div></section></Page>;
}

function Assistant() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [messages, setMessages] = useState<AnyObj[]>([]);
  const [config, setConfig] = useState<AnyObj>({ enabled: false, apiKey: '', modelName: '', baseUrl: '' });
  useEffect(() => { api<AnyObj>('/ai/config').then(setConfig).catch(() => undefined); }, []);
  const quick = ['今日任务总结','明日任务预告','查看风险订单','查看逾期订单','查看产能瓶颈','生成生产日报','哪些订单最急','如果今天没完成怎么办'];
  const ask = async (text = q) => { if (!text.trim()) return; setMessages(m => [...m, {role:'user', text}]); setQ(''); const ans = await api<AnyObj>('/ai/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({question:text})}); setMessages(m => [...m, {role:'assistant', text: ans.answer, links: ans.links}]); };
  const saveConfig = async () => setConfig(await api('/ai/config', { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(config) }));
  return <aside className={`assistant ${open ? 'open' : ''}`}><button className="assistant-toggle" onClick={() => setOpen(!open)}><Bot size={18}/> AI 助手</button>{open && <div className="assistant-body"><div className="quick">{quick.map(x => <button key={x} onClick={() => ask(x)}>{x}</button>)}</div><div className="chat">{messages.map((m,i) => <div key={i} className={m.role}><pre>{m.text}</pre>{m.links?.map((l: AnyObj) => <span className="link-chip" key={l.label}>{l.label}</span>)}</div>)}</div><div className="chat-input"><input value={q} onChange={e => setQ(e.target.value)} onKeyDown={e => e.key==='Enter' && ask()} placeholder="问当前排产、风险、订单完成时间..." /><button onClick={() => ask()}>发送</button></div><details className="ai-config"><summary>AI 配置</summary><label><input type="checkbox" checked={!!config.enabled} onChange={e => setConfig({...config, enabled:e.target.checked})}/> 启用大模型模式</label><input placeholder="API Key" value={config.apiKey || ''} onChange={e => setConfig({...config, apiKey:e.target.value})}/><input placeholder="Model Name" value={config.modelName || ''} onChange={e => setConfig({...config, modelName:e.target.value})}/><input placeholder="Base URL" value={config.baseUrl || ''} onChange={e => setConfig({...config, baseUrl:e.target.value})}/><button onClick={saveConfig}>保存配置</button><small>未配置时使用本地规则助手，所有回答仍基于数据库。</small></details></div>}</aside>;
}

function Page({ title, actions, children }: { title: string; actions?: React.ReactNode; children: React.ReactNode }) {
  return <><header className="page-head"><h1>{title}</h1><div>{actions}</div></header>{children}</>;
}
function Select({ label, value, items, onChange }: { label: string; value: string; items: string[]; onChange: (v: string) => void }) {
  return <select aria-label={label} value={value} onChange={e => onChange(e.target.value)}><option value="">{label}全部</option>{items.map(i => <option key={i} value={i}>{i}</option>)}</select>;
}
function Badge({ v }: { v: string }) { return <span className={`badge ${v}`}>{v}</span>; }
function Empty({ text }: { text: string }) { return <div className="empty">{text}</div>; }
function fmt(n: any) { return Number(n || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 }); }
function weekday(value: string) {
  if (!value) return '-';
  const day = new Date(`${value}T00:00:00`).getDay();
  return `星期${'日一二三四五六'[day]}`;
}
function deadlineLabel(row: AnyObj) {
  const value = String(row.deadline_date || row.latest_finish_date || '');
  const match = value.match(/^\d{4}-(\d{2})-(\d{2})$/);
  if (!match) return '按最晚节点完成';
  return `${Number(match[1])}月${Number(match[2])}日前完成`;
}
function exportFile(path: string) { window.location.href = `${API}${path}`; }
function defaultUnit(process: string) { return process === '开箱' ? '小时' : (['浇筑', '木模造型', '消失模造型'].includes(process) ? '吨' : '件'); }
function cleanOrderPayload(form: AnyObj) {
  return {
    order_no: String(form.order_no || '').trim(),
    model_no: String(form.model_no || '').trim(),
    customer: String(form.customer || '').trim(),
    product_name: String(form.product_name || '').trim(),
    material: String(form.material || '').trim(),
    product_type: form.product_type,
    molding_method: form.molding_method || form.product_type,
    quantity: Number(form.quantity || 0),
    weight_ton: Number(form.weight_ton || 0),
    due_date: form.due_date || null,
    status: form.status || '未开始',
    is_urgent: Boolean(form.is_urgent),
    remark: String(form.remark || '').trim(),
  };
}

createRoot(document.getElementById('root')!).render(<App />);

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Alert, Button, Card, DatePicker, Drawer, Empty, Pagination, Segmented, Spin, Table } from 'antd'
import { monitoring, type DurationRank, type Metric, type MetricResult, type Range, type RecordItem, type Session } from '@/api/monitoring'
import styles from './MonitoringPage.module.css'
import { MetricChart } from '@/features/monitoring/MetricChart'
import { metricLabel } from '@/features/monitoring/format'

const metricOrder: Metric[] = ['tokens', 'success', 'failure', 'skillx', 'workflow', 'duration', 'reasons']
const titles: Record<Metric, string> = { tokens: 'Token 消耗', duration: '任务耗时排行', success: '任务成功率', skillx: 'SkillX 调用量', workflow: 'Workflow 调用量', failure: '任务失败率', reasons: '失败原因排行' }
function recent(hours = 24): Range { const end = new Date(); return { start: new Date(+end - hours * 3600000).toISOString(), end: end.toISOString() } }

function TimeRange({ onChange, revision, hourly = false }: { hourly?: boolean; revision: number; onChange: (range: Range) => void }) {
  const [choice, setChoice] = useState<number | string>(24)
  const change = useRef(onChange)
  change.current = onChange
  useEffect(() => { if (typeof choice === 'number') change.current(recent(choice)) }, [revision, choice])
  return <div className={styles.range}>
    {hourly && <Segmented size="small" aria-label="小时范围" value={choice} options={[{ label: '1小时', value: 1 }, { label: '3小时', value: 3 }, { label: '12小时', value: 12 }]} onChange={setChoice} />}
    <Segmented size="small" aria-label="时间范围" value={choice} options={[{ label: '1天', value: 24 }, { label: '3天', value: 72 }, { label: '7天', value: 168 }, { label: '自定义', value: 'custom' }]}
      onChange={setChoice} />
    {choice === 'custom' && <DatePicker.RangePicker size="small" allowClear={false} onChange={dates => {
      if (dates?.[0] && dates[1]) onChange({ start: dates[0].startOf('day').toISOString(), end: dates[1].add(1, 'day').startOf('day').toISOString() })
    }} />}
  </div>
}

function MetricPanel({ metric, revision }: { metric: Metric; revision: number }) {
  const [range, setRange] = useState(recent)
  const [rankPage, setRankPage] = useState(1)
  const [data, setData] = useState<MetricResult>()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(''); setData(undefined); setRankPage(1)
    monitoring.metric(metric, range, controller.signal).then(value => { if (!controller.signal.aborted) setData(value) }).catch(e => {
      if (!controller.signal.aborted) setError(String(e.message))
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [metric, range, revision])
  const value = data?.value
  const formatted = metricLabel(metric, value ?? null)
  return <Card className={metric === 'reasons' || metric === 'duration' ? styles.wide : ''} title={titles[metric]}>
    <TimeRange hourly={metric === 'tokens'} revision={revision} onChange={setRange} />
    {loading ? <Spin /> : error ? <Alert type="error" title={error} /> : metric === 'duration' ?
      <Table<DurationRank> size="small" rowKey="id" dataSource={data?.ranking} scroll={{ x: 650 }}
        pagination={{ current: rankPage, onChange: setRankPage, pageSize: 5, showSizeChanger: false, hideOnSinglePage: true }} columns={[
          { title: '排名', width: 65, render: (_, _row, index) => (rankPage - 1) * 5 + index + 1 },
          { title: '任务', dataIndex: 'goal', render: (goal: string, row: DurationRank) => <div className={styles.reason}>{row.conversationId ? <Link to={`/chat/${row.conversationId}`}>{goal}</Link> : goal}</div> },
          { title: '状态', dataIndex: 'status', width: 80, render: (status: string) => ({ succeeded: '成功', failed: '失败', blocked: '阻塞' })[status] ?? status },
          { title: '耗时', dataIndex: 'durationMs', width: 120, render: (duration: number) => metricLabel('duration', duration) },
          { title: '创建时间', dataIndex: 'createdAt', width: 180, render: (value: string) => new Date(value).toLocaleString() },
        ]} /> : metric === 'reasons' ?
      <Table size="small" rowKey="reason" dataSource={data?.items} pagination={{ pageSize: 5, hideOnSinglePage: true }} columns={[
        { title: '失败原因', dataIndex: 'reason', render: (text: string, row: { example?: string }) => <span className={styles.reason}>{text}{row.example && row.example !== text && <div className={styles.note}>{row.example}</div>}</span> },
        { title: '次数', dataIndex: 'count', width: 80 },
      ]} /> : <>
      <div className={styles.summary}><span className={styles.value} title={value == null ? undefined : new Intl.NumberFormat('zh-CN').format(value)}>{formatted}</span><span className={styles.note}>{['success', 'failure'].includes(metric) ? '区间总体比例' : '区间合计'}</span></div>
      <MetricChart key={`${metric}-${range.start}-${range.end}`} metric={metric} title={titles[metric]} points={data?.series ?? []} interval={data?.intervalSeconds ?? 3600} />
    </>}
    {data && <div className={styles.note}>{data.basis}
      {data.completed !== undefined && <div>已结束 {data.completed} · 取消 {data.cancelled} · 共 {data.tasks} 个任务</div>}
      {!!data.incompleteTasks && <div>{data.incompleteTasks} 个任务用量尚不完整</div>}
    </div>}
  </Card>
}

function Records({ id }: { id: string }) {
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<{ items: RecordItem[]; nextOffset: number | null }>()
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setData(undefined); setError('')
    monitoring.records(id, offset, controller.signal).then(setData).catch(e => {
      if (!controller.signal.aborted) setError(e.message)
    })
    return () => controller.abort()
  }, [id, offset, revision])
  return <>
    <div className={styles.recordActions}><Button onClick={() => setRevision(x => x + 1)}>刷新记录</Button>
      <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button>
      <Button disabled={data?.nextOffset == null} onClick={() => setOffset(data?.nextOffset ?? 0)}>下一页</Button></div>
    {error ? <Alert type="error" title={error} /> : !data ? <Spin /> : data.items.length === 0 ? <Empty description="暂无记录" /> : data.items.map((item, index) =>
      <details className={styles.record} key={`${offset}-${index}`} open={item.type === 'session_meta' || item.type === 'session_end'}>
        <summary>{offset + index + 1} · {new Date(item.timestamp).toLocaleString()} · {item.type}</summary>
        <pre>{JSON.stringify(item.payload, null, 2)}</pre>
      </details>)}
  </>
}

function Sessions({ revision }: { revision: number }) {
  const [range, setRange] = useState(recent)
  const [page, setPage] = useState(1)
  const [data, setData] = useState<{ items: Session[]; total: number }>()
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<Session>()
  useEffect(() => {
    const controller = new AbortController()
    setData(undefined); setError('')
    monitoring.sessions(range, (page - 1) * 30, controller.signal).then(setData).catch(e => {
      if (!controller.signal.aborted) setError(e.message)
    })
    return () => controller.abort()
  }, [range, page, revision])
  return <Card title="Session 可观测记录" className={styles.full}>
    <TimeRange revision={revision} onChange={value => { setRange(value); setPage(1) }} />
    <p className={styles.note}>每次 Agent 执行保存独立 JSONL：上下文、原始事件、工具参数与结果、结束状态。密钥脱敏。仅新执行产生原始记录；时间按本地时区展示。</p>
    {error ? <Alert type="error" title={error} /> : <Table loading={!data} rowKey="id" dataSource={data?.items} pagination={false} scroll={{ x: 700 }} columns={[
      { title: '时间', dataIndex: 'createdAt', render: (v: string) => new Date(v).toLocaleString() },
      { title: 'Session / 执行', render: (_, row: Session) => <><code>{row.id}</code><div className={styles.note}>{row.runId}</div></> },
      { title: '记录大小', dataIndex: 'bytes', render: (v: number) => `${(v / 1024).toFixed(1)} KB` },
      { title: '操作', render: (_, row: Session) => <Button onClick={() => setSelected(row)}>查看原始记录</Button> },
    ]} />}
    <Pagination current={page} total={data?.total ?? 0} pageSize={30} showSizeChanger={false} onChange={setPage} hideOnSinglePage />
    <Drawer title="Session 原始记录" open={!!selected} onClose={() => setSelected(undefined)} size="large" destroyOnHidden>
      {selected && <Records key={selected.id} id={selected.id} />}
    </Drawer>
  </Card>
}

export default function MonitoringPage() {
  const [revision, setRevision] = useState(0)
  return <div className={styles.page}>
    <header className={styles.header}><div><h1>监控面板</h1><p>用量、执行质量与原始记录。每个面板可独立选择时间。</p></div><Button onClick={() => setRevision(x => x + 1)}>刷新</Button></header>
    <div className={styles.grid}>{metricOrder.map(metric => <MetricPanel key={metric} metric={metric} revision={revision} />)}<Sessions revision={revision} /></div>
  </div>
}

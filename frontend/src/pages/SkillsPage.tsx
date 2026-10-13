import { Alert, App, Button, Empty, Flex, Form, Input, Modal, Popconfirm, Select, Skeleton, Switch, Tag, Typography, Upload } from 'antd'
import { BookOpen, Code2, FolderInput, Pencil, Plus, Sparkles, Trash2, Upload as UploadIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { skillsApi, type Skill, type SkillConversion, type SkillDetail, type SkillInput } from '@/api/skills'
import { DirectoryPicker } from '@/features/chat/DirectoryPicker'
import styles from './skills.module.css'

const blank: SkillInput = {name: '', displayName: '', description: '', content: '', enabled: true}
const pretty = (value: unknown) => JSON.stringify(value, null, 2)
const errorMessage = (reason: unknown) => reason instanceof Error ? reason.message : '操作失败'

export default function SkillsPage() {
  const { message } = App.useApp()
  const [items, setItems] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [modalError, setModalError] = useState('')
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [editor, setEditor] = useState<{id?: string; values: SkillInput; resources?: string[]} | null>(null)
  const [importing, setImporting] = useState(false)
  const [path, setPath] = useState('')
  const [conversion, setConversion] = useState<SkillDetail | null>(null)
  const [kind, setKind] = useState<SkillConversion['kind']>('agent')
  const [inputSchema, setInputSchema] = useState('')
  const [outputSchema, setOutputSchema] = useState('')
  const [source, setSource] = useState('')
  const [examples, setExamples] = useState('[]')

  useEffect(() => {
    skillsApi.list().then(setItems).catch(reason => setError(errorMessage(reason))).finally(() => setLoading(false))
  }, [])
  const refresh = async () => setItems(await skillsApi.list())
  async function edit(item: Skill) {
    setBusy(true); setError(''); setModalError('')
    try {
      const detail = await skillsApi.get(item.id)
      setEditor({id: item.id, values: {name: detail.name, displayName: detail.displayName,
        description: detail.description, content: detail.content, enabled: detail.enabled}, resources: detail.resources})
    } catch (reason) { setError(errorMessage(reason)) } finally { setBusy(false) }
  }
  async function openConversion(item: Skill) {
    setBusy(true); setError(''); setModalError('')
    try {
      const detail = await skillsApi.get(item.id)
      setConversion(detail); setKind(detail.conversion.kind)
      setInputSchema(pretty(detail.conversion.inputSchema)); setOutputSchema(pretty(detail.conversion.outputSchema))
      setSource(detail.conversion.source); setExamples(pretty(detail.conversion.examples))
    } catch (reason) { setError(errorMessage(reason)) } finally { setBusy(false) }
  }
  async function save() {
    if (!editor) return
    setBusy(true); setModalError('')
    try {
      if (editor.id) await skillsApi.update(editor.id, editor.values)
      else await skillsApi.create(editor.values)
      await refresh(); setEditor(null); void message.success('Skill 已保存')
    } catch (reason) { setModalError(errorMessage(reason)) } finally { setBusy(false) }
  }
  async function importDirectory() {
    setBusy(true); setModalError('')
    try { await skillsApi.import(path); await refresh(); setImporting(false); void message.success('Skill 及资源已导入') }
    catch (reason) { setModalError(errorMessage(reason)) } finally { setBusy(false) }
  }
  async function convert() {
    if (!conversion) return
    setBusy(true); setModalError('')
    try {
      await skillsApi.convert(conversion.id, {kind, expectedRevision: conversion.revision,
        inputSchema: kind === 'existing' ? {} : JSON.parse(inputSchema),
        outputSchema: kind === 'existing' ? {} : JSON.parse(outputSchema), source,
        examples: kind === 'direct' ? JSON.parse(examples) : []})
      await refresh(); setConversion(null); void message.success('已转为 SkillX，Agent 将优先使用接口契约')
    } catch (reason) { setModalError(errorMessage(reason)) } finally { setBusy(false) }
  }
  async function toggle(item: Skill, enabled: boolean) {
    setBusy(true); setError('')
    try {
      const detail = await skillsApi.get(item.id)
      await skillsApi.update(item.id, {name: detail.name, displayName: detail.displayName,
        description: detail.description, content: detail.content, enabled})
      await refresh()
    } catch (reason) { setError(errorMessage(reason)) } finally { setBusy(false) }
  }
  const update = (key: keyof SkillInput, value: string) => setEditor(current => current ? {...current, values: {...current.values, [key]: value}} : null)
  const filtered = items.filter(item => (filter === 'all' || filter === item.mode || (filter === 'disabled' && !item.enabled)) &&
    `${item.name} ${item.displayName} ${item.description}`.toLowerCase().includes(query.toLowerCase()))
  return <div className="page">
    <header className="page-header">
      <div><span className="eyebrow">工作空间 / Skills</span><h1>Skills</h1><p>管理 Agent 指令与资源，转为 SkillX 后优先按契约调用。</p></div>
      <Flex gap={8} wrap>
        <Button icon={<FolderInput size={15} />} onClick={() => {setImporting(true); setPath(''); setModalError('')}}>导入目录</Button>
        <Button type="primary" icon={<Plus size={15} />} onClick={() => {setEditor({values: {...blank}}); setModalError('')}}>添加 Skill</Button>
      </Flex>
    </header>
    <div className="collection-toolbar">
      <Input.Search aria-label="搜索 Skill" placeholder="搜索名称或用途" allowClear value={query} onChange={event => setQuery(event.target.value)} style={{maxWidth: 300}} />
      <div className="filter-strip">{[['all', '全部'], ['skill', '原始 Skill'], ['skillx', 'SkillX'], ['disabled', '已停用']].map(([value, label]) =>
        <Button key={value} className={`filter-chip ${filter === value ? 'active' : ''}`} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</Button>)}</div>
    </div>
    {loading && <Skeleton active paragraph={{rows: 4}} />}
    {error && <Alert type="error" title={error} showIcon />}
    {!loading && !error && !filtered.length && <Empty description={items.length ? '没有匹配的 Skill' : '添加 Skill 指令，或导入已有的 Skill 目录'} />}
    <section className="component-grid">
      {filtered.map(item => <article className={`component-card ${!item.enabled ? styles.disabled : ''}`} key={item.id}>
        <button className={`workflow-card-main ${styles.cardMain}`} onClick={() => void edit(item)} aria-label={`编辑 ${item.displayName}`}>
          <div className="card-topline"><span className="component-icon">{item.mode === 'skillx' ? <Sparkles size={17} /> : <BookOpen size={17} />}</span>
            <Tag>{item.mode === 'skillx' ? `SkillX · ${item.interfaceKind === 'direct' ? '脚本' : 'Agent'}` : '原始 Skill'}</Tag><Pencil size={15} /></div>
          <h2>{item.displayName}</h2><p>{item.description}</p>
        </button>
        <div className={styles.version}>{item.name} · {item.skillxVersion ? `v${item.skillxVersion}` : `修订 ${item.revision}`}</div>
        <div className={`card-metrics ${styles.actions}`}>
          <Switch size="small" checked={item.enabled} disabled={busy} checkedChildren="启用" unCheckedChildren="停用" aria-label={`启用 ${item.displayName}`} onChange={enabled => void toggle(item, enabled)} />
          <Flex gap={4}>
            <Button size="small" type="text" disabled={busy} icon={<Code2 size={14} />} onClick={() => void openConversion(item)}>{item.mode === 'skillx' ? '更新 SkillX' : '转为 SkillX'}</Button>
            <Popconfirm title="删除此 Skill？" description="停止向 Agent 提供此 Skill；已有工作流绑定的版本会保留。" onConfirm={async () => {
              try { await skillsApi.remove(item.id); await refresh() } catch (reason) { setError(errorMessage(reason)) }
            }}><Button type="text" size="small" aria-label={`删除 ${item.displayName}`} icon={<Trash2 size={14} />} /></Popconfirm>
          </Flex>
        </div>
      </article>)}
    </section>
    <Modal title={editor?.id ? '编辑 Skill' : '添加 Skill'} open={!!editor} width={760} onCancel={() => {if (!busy) setEditor(null)}} onOk={() => void save()} confirmLoading={busy} okText="保存" cancelText="取消" destroyOnHidden>
      <Form layout="vertical">
        <Flex gap={16}><Form.Item label="标识" required style={{flex: 1}}><Input aria-label="Skill 标识" placeholder="如 summarize-report" value={editor?.values.name} onChange={event => update('name', event.target.value)} /></Form.Item>
          <Form.Item label="名称" required style={{flex: 1}}><Input aria-label="Skill 名称" value={editor?.values.displayName} onChange={event => update('displayName', event.target.value)} /></Form.Item></Flex>
        <Form.Item label="用途" required><Input.TextArea aria-label="Skill 用途" rows={2} value={editor?.values.description} onChange={event => update('description', event.target.value)} /></Form.Item>
        <Form.Item label={<Flex align="center" gap={12}>Skill 指令 / SKILL.md <Upload showUploadList={false} accept=".md" beforeUpload={async file => {
          try {
            if (file.size > 128 * 1024) throw new Error('SKILL.md 不能超过 128 KB')
            const content = await file.text()
            const metadata = await skillsApi.preview(content)
            setEditor(current => current ? {...current, values: {...current.values, content,
              name: current.values.name || metadata.name,
              displayName: current.values.displayName || metadata.displayName || metadata.name,
              description: current.values.description || metadata.description,
            }} : null)
          } catch (reason) { setModalError(errorMessage(reason)) }
          return false
        }}><Button size="small" icon={<UploadIcon size={13} />}>读取文件</Button></Upload></Flex>} required>
          <Input.TextArea className={styles.code} aria-label="Skill 指令" rows={12} value={editor?.values.content} onChange={event => update('content', event.target.value)} placeholder="描述适用条件、执行步骤和输出要求，支持完整 SKILL.md。" />
        </Form.Item>
        {editor?.resources && <Typography.Paragraph type="secondary">资源：{editor.resources.join('、')}</Typography.Paragraph>}
        {editor?.id && <Typography.Paragraph type="secondary">修改指令或用途后需重新转换 SkillX；原有工作流仍使用已绑定的版本。</Typography.Paragraph>}
        {modalError && <Alert type="error" title={modalError} showIcon />}
      </Form>
    </Modal>
    <Modal title="导入 Skill 目录" open={importing} onCancel={() => {if (!busy) setImporting(false)}} onOk={() => void importDirectory()} confirmLoading={busy} okText="导入" cancelText="取消" okButtonProps={{disabled: !path.trim()}}>
      <Typography.Paragraph type="secondary">选择服务所在电脑上包含 SKILL.md 的目录，脚本与参考文件会一并保存为资源快照。同名 Skill 或已关联的 SkillX 将更新原 Skill；转换后发布为同一能力的新版本。</Typography.Paragraph>
      <Flex gap={8}><Input aria-label="Skill 目录路径" placeholder="/path/to/skill" value={path} onChange={event => setPath(event.target.value)} /><DirectoryPicker value={path || undefined} disabled={busy} onChange={value => setPath(value || '')} /></Flex>
      {modalError && <Alert style={{marginTop: 16}} type="error" title={modalError} showIcon />}
    </Modal>
    <Modal title={`转为 SkillX · ${conversion?.displayName || ''}`} open={!!conversion} width={880} onCancel={() => {if (!busy) setConversion(null)}} onOk={() => void convert()} confirmLoading={busy} okText={kind === 'direct' ? '验证脚本并转换' : '校验契约并转换'} cancelText="取消" destroyOnHidden>
      <Form layout="vertical">
        <Form.Item label="执行方式"><Select aria-label="SkillX 执行方式" value={kind} onChange={setKind} options={[
          ...(conversion?.conversion.hasManifest ? [{value: 'existing', label: '复用已有 SkillX 契约与依赖'}] : []),
          {value: 'direct', label: '直接执行 Python 脚本（优先）'}, {value: 'agent', label: 'Agent 接口（语义任务）'},
        ]} /></Form.Item>
        <Alert showIcon type="info" title={kind === 'direct' ? '脚本必须提供 execute(request, context)，通过 request.input 读取输入，返回符合输出契约的对象。至少两组不同输入需验证通过。' : kind === 'agent' ? '只有实际调用接口时，内部 Agent 才读取 Skill 指令与 Markdown 参考材料。该接口使用模型，不额外授予脚本、联网或其他工具权限。' : '校验已有 SkillX 契约，保留接口、资源和已声明的依赖。'} />
        {kind !== 'existing' && <>
          <div className={styles.schemas}>
            <Form.Item label="输入契约（JSON Schema）" required><Input.TextArea aria-label="输入契约" className={styles.code} rows={9} value={inputSchema} onChange={event => setInputSchema(event.target.value)} /></Form.Item>
            <Form.Item label="输出契约（JSON Schema）" required><Input.TextArea aria-label="输出契约" className={styles.code} rows={9} value={outputSchema} onChange={event => setOutputSchema(event.target.value)} /></Form.Item>
          </div>
          {kind === 'direct' && <>
            <Form.Item label="Python 脚本" required><Input.TextArea aria-label="Python 脚本" className={styles.code} rows={10} value={source} onChange={event => setSource(event.target.value)} /></Form.Item>
            <Form.Item label="验证样例（JSON 数组，每项包含 input 与 expected）" required><Input.TextArea aria-label="验证样例" className={styles.code} rows={6} value={examples} onChange={event => setExamples(event.target.value)} placeholder={'[{"input": {"x": 1}, "expected": {"answer": "1"}}, ...]'} /></Form.Item>
          </>}
        </>}
        {modalError && <Alert type="error" title={modalError} showIcon />}
      </Form>
    </Modal>
  </div>
}

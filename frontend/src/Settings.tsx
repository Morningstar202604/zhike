import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  SlidersHorizontal, BookOpen, BarChart3, Brain, ShieldAlert,
  Save, Plus, Pencil, Trash2, X, Activity, Inbox, MessageSquare,
  FileText, Users, Search, Wrench, Zap, ChevronDown, ChevronUp,
  ChevronLeft, ChevronRight, Upload, Download, KeyRound, Check,
  Plug, Wand2, Lock, MessagesSquare } from 'lucide-react'
import { ProviderPanel, SkillsPanel, HooksPanel, PermissionsPanel } from './ProviderCenter'
import {
  useConfig, useUpdateConfig, useKb, useKbMutations, useKbImport, useKbTest, useKbDocImport, exportKb, useTools, useToolMutations,
  useInjectionRules, useInjectionRuleMutations, useLlmTest, useLlmModels,
  useAdminOverview, useAdminMemory, useAdminInjection, useAdminReport, useAdminStats,
  useProfiles, useProfileMutations, useLexicon, useLexiconMutations,
  useAdminSchema, useAdminPurge,
} from './lib/queries'
import { StatCard, EmptyState } from './ui'
import type { Profile, LexiconEntry } from './lib/queries'
import { useShell } from './components/ResponsiveShell'
import { Spinner, ErrorBanner } from './components/Spinner'
import type { AgentConfig, KbEntry, ToolDef, InjectionRule, MemoryEntry, InjectionLogEntry } from './types/api'
import { usePagination } from './lib/pagination'
import { Grp, Row, Pager } from './settings/bits'
import { ParamsPanel } from './settings/panels-core'
import { ToolsPanel, GuardrailPanel } from './settings/panels-core'
import { KbPanel } from './settings/panels-kb'
import { DataPanel, MemPanel, InjPanel, TokenPanel } from './settings/panels-data'
import { isAuthError } from './lib/queries'
import { TOOL_LABEL } from './lib/labels'
import TokenGate from './components/TokenGate'
import { useAppStore } from './store/appStore'
import { useFeedback } from './components/Feedback'
import CardsPanel from './CardsPanel'
import './pages2.css'
import { t as tr } from './lib/t'

const TAB_GROUPS: { group: string; items: { key: string; icon: React.ReactNode; label: string; sub: string }[] }[] = [
  {
    group: '模型与推理',
    items: [
      { key: 'providers', icon: <Plug size={15} />, label: '模型厂商', sub: '地址 + Key 自动检测模型与能力 · 三协议' },
      { key: 'params', icon: <SlidersHorizontal size={15} />, label: '能力参数', sub: '模型 / 检索 / 上下文 / 记忆 / 引擎' },
    ],
  },
  {
    group: '行为与知识',
    items: [
      { key: 'skills', icon: <Wand2 size={15} />, label: '技能', sub: '什么时候用哪个工具' },
      { key: 'tools', icon: <Wrench size={15} />, label: '工具注册表', sub: '可自定义 Agent 能执行的动作' },
      { key: 'cards', icon: <Brain size={15} />, label: '角色卡', sub: 'SillyTavern 卡导入 / 按群绑定人格' },
      { key: 'profiles', icon: <Users size={15} />, label: '档案', sub: '人物与群：备注/标签，机器人靠它认人' },
      { key: 'lexicon', icon: <MessagesSquare size={15} />, label: '黑话表', sub: '群里的梗与缩写，机器人靠它听懂黑话' },
      { key: 'mem', icon: <Brain size={15} />, label: '长期记忆', sub: '跨会话沉淀' },
    ],
  },
  {
    group: '安全与合规',
    items: [
      { key: 'permission', icon: <Lock size={15} />, label: '权限', sub: '全面放开 / 核心锁定' },
      { key: 'guardrail', icon: <ShieldAlert size={15} />, label: '注入规则', sub: '护栏检测规则管理' },
      { key: 'hooks', icon: <ShieldAlert size={15} />, label: '钩子', sub: '管线固定位置的强制规则' },
      { key: 'inj', icon: <Activity size={15} />, label: '注入日志', sub: '护栏审计' },
    ],
  },
  {
    group: '数据与运维',
    items: [
      { key: 'data', icon: <BarChart3 size={15} />, label: '数据概览', sub: '运营统计 / 存储维护' },
      { key: 'token', icon: <KeyRound size={15} />, label: '访问令牌', sub: '控制台登录凭据' },
    ],
  },
]
// 平铺视图供 active 查找与页头：必须包含 kb（/settings/kb 是一级导航入口）。
// TAB_GROUPS 只决定侧栏展示，不含 kb —— 双入口去重只发生在侧栏，URL 与内容保留。
const KB_TAB = { key: 'kb', icon: <BookOpen size={15} />, label: '知识库', sub: 'RAG 语料增删改' }
const TABS = [...TAB_GROUPS.flatMap((g) => g.items), KB_TAB]

export default function Settings() {
  const { tab: routeTab } = useParams()
  const navigate = useNavigate()
  const routeOk = TABS.some((t) => t.key === routeTab)
  const tab = routeOk ? (routeTab as string) : 'params'
  const setTab = (k: string) => navigate(`/settings/${k}`)
  const { mobile } = useShell()
  const { data: cfgRes, isLoading, error, refetch } = useConfig()
  const cfg = cfgRes?.config
  const upd = useUpdateConfig()
  // 配置草稿：所有控件只改本地状态，点「保存配置」才落库。
  // 之前每个 onChange 都 PUT 整包，滑杆拖动=请求风暴，响应乱序还会覆盖新值。
  const [draft, setDraft] = useState<AgentConfig | null>(null)
  useEffect(() => {
    setDraft(cfgRes?.config ?? null)
  }, [cfgRes])
  const shown = draft ?? cfg

  if (isLoading) return <Spinner full label={tr('加载配置…')} />
  // 401/403/503 都以令牌门禁承接：以前这里渲染 ErrorBanner，用户无法就地登录
  if (error && isAuthError(error)) return <TokenGate error={error} onAuthed={() => refetch()} />
  if (error) return <div className="p-8"><ErrorBanner message={(error as Error).message} onRetry={() => refetch()} /></div>
  if (!cfg) return <TokenGate onAuthed={() => refetch()} />

  const active = TABS.find((t) => t.key === tab)!

  return (
    <div className={`set-app ${mobile ? 'set-app-mobile' : ''}`}>
      <aside className="set-side">
        <div className="ss-title">{tr('管理中心')}</div>
        <div className="ss-sub">{tr('Agent 运行时配置 · 即时生效')}</div>
        {TAB_GROUPS.map((g) => (
          <div key={g.group} className="set-group">
            <div className="set-group-label">{tr(g.group)}</div>
            {g.items.map((item) => (
              <button key={item.key} className={`set-item ${tab === item.key ? 'on' : ''}`} onClick={() => setTab(item.key)}>
                <span className="si-ico">{item.icon}</span>
                <span>{tr(item.label)}</span>
              </button>
            ))}
          </div>
        ))}
      </aside>

      <main className="set-main">
        <div className="set-main-head">
          <div>
            <h1>{tr(active.label)}</h1>
            <p>{tr(active.sub)}</p>
          </div>
          {(tab === 'params' || tab === 'providers') && (
            <button className="btn" onClick={() => shown && upd.mutate(shown)}
                    disabled={upd.isPending || !shown}>
              <Save size={14} />
              {upd.isPending ? '保存中…' : upd.isSuccess ? '已保存' : '保存配置'}
            </button>
          )}
        </div>

        {tab === 'params' && upd.isError && (
          <div className="mb-3">
            <ErrorBanner message={(upd.error as Error).message} onRetry={() => upd.mutate(shown!)} />
          </div>
        )}
        {tab === 'providers' && shown && (
          <ProviderPanel cfg={shown} setCfg={(next) => setDraft(next)} persist={(next) => upd.mutate(next)} />
        )}
        {tab === 'skills' && <SkillsPanel />}
        {tab === 'hooks' && <HooksPanel />}
        {tab === 'permission' && <PermissionsPanel />}
        {tab === 'params' && shown && <ParamsPanel cfg={shown} setDraft={setDraft} upd={upd} />}
        {tab === 'cards' && <CardsPanel />}
        {tab === 'tools' && <ToolsPanel />}
        {tab === 'guardrail' && <GuardrailPanel />}
        {tab === 'kb' && <KbPanel />}
        {tab === 'data' && <DataPanel />}
        {tab === 'mem' && <MemPanel />}
        {tab === 'profiles' && <ProfilesPanel />}
        {tab === 'lexicon' && <LexiconPanel />}
        {tab === 'inj' && <InjPanel />}
        {tab === 'token' && <TokenPanel />}
      </main>
    </div>
  )
}

/* ============ 参数配置（补齐 char_budget/react_steps/top_p + 引擎模式） ============ */
function ProfilesPanel() {
  const { data, isLoading } = useProfiles()
  const { save } = useProfileMutations()
  const fb = useFeedback()
  const items: Profile[] = data?.profiles ?? []
  const pg = usePagination(items)
  const [editing, setEditing] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [alias, setAlias] = useState('')

  if (isLoading) return <Spinner full label={tr('加载档案…')} />
  if (!items.length) return <EmptyState icon={<Users size={22} />} title={tr('还没有档案')} desc={tr('群里有人说话后自动建档；也可以手动添加备注。')} />

  const startEdit = (p: Profile) => {
    setEditing(p.key); setNote(p.note || ''); setAlias(p.alias || '')
  }
  const commit = (key: string) => {
    save.mutate({ key, note, alias }, {
      onSuccess: () => { setEditing(null); fb.toast.success(tr('档案已保存')) },
      onError: (e) => fb.toast.error(`${tr('保存失败')}: ${(e as Error).message}`),
    })
  }

  return (
    <>
      <div className="tbl-wrap">
        <table className="dt">
          <thead><tr><th>{tr('标识')}</th><th>{tr('类型')}</th><th>{tr('昵称')}</th><th>{tr('别名')}</th><th>{tr('互动')}</th><th>{tr('备注')}</th><th></th></tr></thead>
          <tbody>
            {pg.pageItems.map((p) => (
              <tr key={p.key}>
                <td className="raw" title={p.key}>{p.key}</td>
                <td><span className="badge">{p.kind}</span></td>
                <td>{p.display_name || '—'}</td>
                <td>
                  {editing === p.key
                    ? <input value={alias} onChange={(e) => setAlias(e.target.value)} style={{ width: 100 }} />
                    : (p.alias || '—')}
                </td>
                <td>{Object.values(p.stats || {}).reduce((a: number, b) => a + Number(b || 0), 0)}</td>
                <td>
                  {editing === p.key
                    ? <input value={note} onChange={(e) => setNote(e.target.value)} style={{ width: 160 }} />
                    : <span className="raw" title={p.note}>{p.note || '—'}</span>}
                </td>
                <td>
                  {editing === p.key
                    ? <>
                        <button className="btn sm" onClick={() => commit(p.key)} disabled={save.isPending}>{tr('存')}</button>
                        <button className="btn ghost sm" onClick={() => setEditing(null)}>{tr('取消')}</button>
                      </>
                    : <button className="btn ghost sm" onClick={() => startEdit(p)}>{tr('编辑')}</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pg.hasPages && <Pager page={pg.page} maxPage={pg.maxPage} total={pg.total} onChange={pg.setPage} label={tr('档案')} />}
    </>
  )
}

/* ============ 黑话表：后端 /api/lexicon ============ */
function LexiconPanel() {
  const { data, isLoading } = useLexicon()
  const { add } = useLexiconMutations()
  const fb = useFeedback()
  const items: LexiconEntry[] = data?.entries ?? []
  const pg = usePagination(items)
  const [term, setTerm] = useState('')
  const [meaning, setMeaning] = useState('')

  const submit = () => {
    if (!term.trim()) return
    add.mutate({ scope: 'global', term: term.trim(), meaning: meaning.trim() }, {
      onSuccess: () => { setTerm(''); setMeaning(''); fb.toast.success(tr('黑话已记录')) },
      onError: (e) => fb.toast.error(`${tr('保存失败')}: ${(e as Error).message}`),
    })
  }

  return (
    <>
      <div className="f-row" style={{ marginBottom: 10 }}>
        <span className="f-lb">{tr('新增')}</span>
        <div className="flex items-center gap-2 flex-wrap">
          <input value={term} onChange={(e) => setTerm(e.target.value)} placeholder={tr('黑话，如：绝绝子')} maxLength={40} style={{ width: 160 }} />
          <input value={meaning} onChange={(e) => setMeaning(e.target.value)} placeholder={tr('释义')} maxLength={120} style={{ width: 220 }} />
          <button className="btn sm" onClick={submit} disabled={!term.trim() || add.isPending}>{add.isPending ? tr('保存中…') : tr('添加')}</button>
        </div>
      </div>
      {isLoading ? <Spinner full label={tr('加载黑话表…')} /> : !items.length ? (
        <EmptyState icon={<MessagesSquare size={22} />} title={tr('黑话表还是空的')} desc={tr('群里出现的黑话会自动沉淀到这里待补充；也可以手动添加。')} />
      ) : (
        <>
          <div className="tbl-wrap">
            <table className="dt">
              <thead><tr><th>{tr('黑话')}</th><th>{tr('释义')}</th><th>{tr('出现次数')}</th><th>{tr('更新时间')}</th></tr></thead>
              <tbody>
                {pg.pageItems.map((e: LexiconEntry) => (
                  <tr key={e.id}>
                    <td><b>{e.term}</b></td>
                    <td className="raw">{e.meaning || tr('（待补充）')}</td>
                    <td>{e.hits}</td>
                    <td>{(e.updated_at || '').slice(0, 16).replace('T', ' ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pg.hasPages && <Pager page={pg.page} maxPage={pg.maxPage} total={pg.total} onChange={pg.setPage} label={tr('黑话表')} />}
        </>
      )}
    </>
  )
}


/* ============ 访问令牌（本机自动获取 + 可视化复制） ============ */

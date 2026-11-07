import React, { DragEvent, FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';
import './workbench-redesign.css';
import TracePage from './TracePage';
import {decisionSummary, eventTitle, reportSummary} from './trace-model';
import {runtimeErrorMessage} from './runtime-error';

type Row = Record<string, any>;
type Workspace = { id: string; name: string; path: string };
type ConversationMessage = { message_id: string; session_id: string; task_id?: string | null; role: 'user' | 'assistant' | string; kind: 'user' | 'progress' | 'question' | 'final' | string; content: string; seq: number; created_at: string };
type Session = Row & { session_id: string; title?: string; repo_path?: string; repository?: Row; tasks?: Row[]; messages?: ConversationMessage[] };
const WORKSPACES_KEY = 'evocode.workspaces.v1';
const ATTACHMENT_MARKER = '\n\n--- EvoCode attachment: ';
const messageTimeFormat = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
type Attachment = { id: string; name: string; size: number; content: string };
const labels: Record<string, string> = {
  created: '排队中', planning: '规划中', running: '执行中', replanning: '重新规划', repairing: '根据检查结果修复中', waiting_user: '等待你回复',
  completed: '已完成', failed: '失败', cancelled: '已取消', needs_review: '待核对', interrupted: '已中断',
};
const utilityLabels: Record<string, string> = { 记忆: '过去任务', 技能: '经验库', 进化: '经验整理', 配置: '设置' };
const utilityIcons: Record<string, string> = { 记忆: '◷', 技能: '▤', 进化: '↗', 配置: '⚙' };
const utilityDescriptions: Record<string, string> = { 记忆: '找以前的对话', 技能: '查看 Agent 的经验', 进化: '整理和检查经验', 配置: '选择 Agent 的能力' };
const terminal = new Set(['completed', 'failed', 'cancelled']);
const executableStatuses = new Set(['created', 'planning', 'running', 'replanning', 'repairing']);
const connectionError = '暂时无法连接到本地服务，正在重试…';

async function api(path: string, method = 'GET', body?: unknown) {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error instanceof TypeError) throw new Error(connectionError);
    throw error;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail ?? response.statusText));
  return data;
}

function readWorkspaces(): Workspace[] {
  try {
    const value = JSON.parse(localStorage.getItem(WORKSPACES_KEY) || '[]');
    return Array.isArray(value) ? value.filter((item) => item && typeof item.id === 'string' && typeof item.path === 'string') : [];
  } catch { return []; }
}
function shortPath(value: string) { return value.replace(/\/$/, '').split('/').filter(Boolean).slice(-2).join('/') || value; }
const evidenceCheckLabels: Record<string, string> = {
  repository: '工作区', git: '代码版本', paths: '文件路径', symbols: '函数和类', dependencies: '项目依赖', content: '文件内容',
};
const evidenceCheckStatus: Record<string, string> = { valid: '一致', stale: '有变化', unknown: '缺少比对依据' };

function MemoryEvidence({item}: {item: Row}) {
  const metadata = item.metadata || {};
  const checks = Object.entries(metadata.applicability?.checks || {}) as [string, Row][];
  const episode = metadata.chunk_type === 'episode' && Array.isArray(metadata.event_ids);
  const status = item.state_validity === 'valid' ? '记录仍适用' : item.state_validity === 'stale' ? '记录可能过时' : '适用性未确认';
  return <article className="memory-result">
    <div><b>{episode ? `排障过程 · ${metadata.event_ids.length} 条原始事件 · ` : ''}{status}</b><a href={`#session=${item.session_id}`}>查看来源对话 ↗</a></div>
    <p>{item.text || item.content || '原记录没有文本内容。'}</p>
    {checks.length > 0 && <details className="memory-applicability">
      <summary>查看适用性检查</summary>
      <dl>{checks.map(([key, check]) => <React.Fragment key={key}>
        <dt>{evidenceCheckLabels[key] || key}</dt><dd>{evidenceCheckStatus[check.status] || '缺少比对依据'}</dd>
      </React.Fragment>)}</dl>
    </details>}
  </article>;
}
function sessionTitle(row: Row) {
  const saved = String(row?.title || row?.name || '').trim();
  if (saved && !['对话', '新对话'].includes(saved)) return saved;
  const latest = [...(row?.tasks || [])].sort((a: Row, b: Row) => String(b.created_at || '').localeCompare(String(a.created_at || '')))[0];
  return String(latest?.prompt || '').trim().slice(0, 56) || '新对话';
}
function relativeTime(value?: string) {
  if (!value) return '';
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60000));
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
  return `${Math.floor(minutes / 1440)} 天前`;
}
function eventHumanTitle(event: Row) {
  const payload = event.payload || {};
  const toolNames: Record<string, string> = { read_file: '读取文件', edit_file: '修改文件', run_command: '运行命令', run_tests: '运行测试', memory_search: '搜索过去任务' };
  const toolStates: Record<string, string> = {'tool.started':'开始','tool.completed':'完成','tool.failed':'失败','tool.resolved':'已核对'};
  if (event.event_type.startsWith('tool.')) return `${toolNames[payload.tool] || payload.tool || '工具'} · ${toolStates[event.event_type] || '已更新'}`;
  const titles: Record<string, string> = {
    'user.message': '收到你的消息', 'user.message.consumed': '已收到补充要求', 'planner.plan': '整理任务步骤',
    'planner.replanned': '根据新要求调整步骤', 'planner.decision': '规划器决定下一步', 'coder.message': payload.message?.tool_calls?.length ? '准备调用工具' : 'Agent 回复', 'coder.report': '编码器汇报进度', 'assistant.question': '等待你回答', 'node.transition': '切换执行节点',
    'memory.retrieved': '查找过去任务', 'skill.loaded': '读取相关经验', 'verification.result': '项目检查结果', 'verification.selected':'已选择项目检查',
    'verification.repair_requested': '检查未通过，继续修复', 'task.error': '执行遇到问题',
    'plan.step.started': '开始处理一个步骤', 'plan.step.completed': '完成一个步骤',
    'context.assembled': '准备任务所需信息', 'context.compacted': '整理任务信息',
  };
  if (event.event_type === 'task.updated') return ({completed:'任务完成',failed:'任务失败',cancelled:'任务已停止',needs_review:'等待你核对',interrupted:'任务中断',waiting_user:'等待你回复'} as Row)[payload.status] || '任务状态更新';
  return titles[event.event_type] || eventTitle(event);
}
function eventHumanDetail(event: Row) {
  const p = event.payload || {};
  if (event.event_type === 'delivery.target_blocked') return `指定目录与当前工作区不一致，请先确认工作区。目标：${(p.requested_paths || []).join('、')}`;
  if (event.event_type === 'delivery.verification') return p.status === 'passed' ? '已核对交付文件的位置和实际写入记录。' : p.status === 'not_applicable' ? '本次没有需要核对的明确交付路径。' : `尚未确认文件已写入指定位置：${(p.missing_writes || []).join('、')}`;
  if (event.event_type === 'delivery.repair_requested') return '交付位置尚未核对通过，正在补齐文件。';
  if (event.event_type.startsWith('coder.report.') && event.event_type !== 'coder.report') return '正在纠正执行报告的格式，已完成的工具操作不会重做。';
  if (event.event_type === 'user.message' || event.event_type === 'user.message.consumed') return p.content || p.message || '已收到你的要求。';
  if (event.event_type === 'planner.plan' || event.event_type === 'planner.replanned') return p.plan?.goal || p.reason || 'Agent 已梳理任务并安排了执行步骤。';
  if (event.event_type === 'planner.decision') return decisionSummary(p);
  if (event.event_type === 'coder.report') return reportSummary(p);
  if (event.event_type === 'assistant.question') return p.content || p.question || p.summary || 'Agent 需要你补充信息。';
  if (event.event_type === 'node.transition') return `${p.from_node || p.from || '当前节点'} → ${p.to_node || p.to || '下一节点'}`;
  if (event.event_type === 'coder.message') return p.message?.content || (p.message?.tool_calls?.length ? `Agent 准备调用 ${p.message.tool_calls.length} 项工具。` : 'Agent 正在处理任务。');
  if (event.event_type === 'tool.started') {
    const args = p.arguments || {};
    if (p.tool === 'run_command') return `开始运行：${Array.isArray(args.command) ? args.command.join(' ') : args.command || '项目命令'}`;
    if (p.tool === 'read_file' || p.tool === 'edit_file') return `${p.tool === 'read_file' ? '读取' : '修改'}文件：${args.path || '文件路径'}`;
    return '开始执行这项操作。';
  }
  if (event.event_type === 'tool.completed' || event.event_type === 'tool.resolved') {
    const output = p.output || p.result;
    if (!output) return '操作已完成。';
    if (typeof output !== 'string') return '操作已完成，详细结果见完整轨迹。';
    try { if (output.trim().startsWith('{') || output.trim().startsWith('[')) { JSON.parse(output); return '操作已完成，结构化结果见完整轨迹。'; } return output.slice(0, 180) + (output.length > 180 ? '…（完整结果见轨迹）' : ''); }
    catch { return output.slice(0, 180) + (output.length > 180 ? '…（完整结果见轨迹）' : ''); }
  }
  if (event.event_type === 'tool.failed' || event.event_type === 'task.error') return p.error || p.message || p.output || '这一步没有成功。';
  if (event.event_type === 'plan.step.started' || event.event_type === 'plan.step.completed') return p.goal || p.result || p.step_id || '计划步骤';
  if (event.event_type === 'code.patch') return `修改文件：${(p.file_paths || []).join('、') || '文件信息未记录'}`;
  if (event.event_type === 'context.assembled') return `${String(p.role || p.node || '').toLowerCase().includes('planner') ? '规划器' : '编码器'}收到 ${p.message_count ?? '—'} 条消息、${p.evidence_count ?? 0} 条历史证据和 ${p.skill_count ?? 0} 项技能${p.artifact_id ? `，快照 ${p.artifact_id}` : ''}。`;
  if (event.event_type === 'context.compacted') return p.summary || '整理了较早的执行记录。';
  if (event.event_type === 'memory.retrieved') return `已查找相关任务，找到 ${p.evidence_event_ids?.length || 0} 条记录。`;
  if (event.event_type === 'skill.loaded') return '已为本次任务读取相关经验。';
  if (event.event_type === 'verification.selected') return (p.checks || []).map((check: Row) => `${check.name}：${check.command?.join(' ') || ''}`).join('；') || '正在识别适合项目的检查。';
  if (event.event_type === 'verification.result') return p.status === 'passed' ? p.coverage === 'syntax_only' ? '语法检查通过，尚未进行功能测试。' : `项目检查通过${p.checks?.length ? `（${p.checks.length} 项）` : ''}。` : p.status === 'failed' ? '项目检查未通过，Agent 将根据实际结果修复。' : p.reason || '尚未完成项目检查。';
  if (event.event_type === 'verification.repair_requested') return '检查没有通过，Agent 根据结果继续修复。';
  if (event.event_type === 'task.updated') return eventHumanTitle(event);
  return p.summary || p.content || '已记录这一步。';
}
function eventDetailText(event: Row) {
  const detail = eventHumanDetail(event);
  return typeof detail === 'string' ? detail : JSON.stringify(detail, null, 2);
}

function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionQuery, setSessionQuery] = useState('');
  const [selected, setSelected] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get('session') || '');
  const [conversation, setConversation] = useState<Row | null>(null);
  const [viewTab, setViewTab] = useState<'conversation' | 'trace' | 'graph'>('conversation');
  const [task, setTask] = useState<Row | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [stopPending, setStopPending] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [draggingFiles, setDraggingFiles] = useState(false);
  const [workspaces, setWorkspaces] = useState<Workspace[]>(readWorkspaces);
  const [activeWorkspace, setActiveWorkspace] = useState('');
  const [workspaceDialog, setWorkspaceDialog] = useState(false);
  const [draftName, setDraftName] = useState('');
  const [draftPath, setDraftPath] = useState('');
  const [utility, setUtility] = useState('');
  const [utilityData, setUtilityData] = useState<any>(null);
  const [utilityDetail, setUtilityDetail] = useState<any>(null);
  const [utilityLoading, setUtilityLoading] = useState(false);
  const [utilityError, setUtilityError] = useState('');
  const [configText, setConfigText] = useState('');
  const [settingsDraft, setSettingsDraft] = useState<Row | null>(null);
  const [specText, setSpecText] = useState('');
  const sendLockRef = useRef(false);
  const composingRef = useRef(false);
  const compositionEndedAtRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const scrollNearBottomRef = useRef(true);
  const scrollSnapshotRef = useRef({ sessionId: '', messageIds: [] as string[] });

  const active = workspaces.find((item) => item.id === activeWorkspace) || null;
  const sessionRepoPath = conversation?.session?.repo_path || conversation?.session?.repository?.root_path || '';
  const sessionWorkspace: Workspace | null = selected && sessionRepoPath ? { id: 'session-workspace', name: shortPath(sessionRepoPath), path: sessionRepoPath } : null;
  const currentWorkspace = active || sessionWorkspace;
  const currentStatus = task?.status || '';
  const executionActive = executableStatuses.has(currentStatus);
  const state = task?.state || {};
  const events: Row[] = conversation?.events || [];
  const conversationTasks: Row[] = conversation?.tasks || [];
  const activeTask = conversationTasks.find((row) => !terminal.has(row.status) && row.status !== 'interrupted') || null;
  const currentStep = state.current_step || state.plan?.current_step || state.plan?.steps?.find((step: Row) => step.status === 'in_progress') || null;
  const filteredSessions = useMemo(() => (active ? sessions.filter((row) => {
    const path = row.repo_path || row.repository?.root_path || row.repository?.repo_path;
    return path === active.path;
  }) : sessions).filter((row) => !sessionQuery.trim() || String(row.title || row.name || '').toLowerCase().includes(sessionQuery.trim().toLowerCase())), [sessions, active, sessionQuery]);

  useEffect(() => { window.history.replaceState(null, '', selected ? '#session=' + encodeURIComponent(selected) : window.location.pathname); }, [selected]);
  useEffect(() => { setViewTab('conversation'); }, [selected]);
  const refresh = async () => {
    setSessions(await api('/sessions'));
    setError((current) => current === connectionError ? '' : current);
  };
  useEffect(() => {
    refresh().catch((e) => setError(e.message));
    const timer = window.setInterval(() => refresh().catch(() => {}), 3000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    localStorage.setItem(WORKSPACES_KEY, JSON.stringify(workspaces));
    if (activeWorkspace && !workspaces.some((item) => item.id === activeWorkspace)) setActiveWorkspace('');
  }, [workspaces, activeWorkspace]);
  useEffect(() => {
    if (!selected) { setConversation(null); setTask(null); return; }
    let live = true;
    let source: EventSource | null = null;
    let streamTaskId = '';
    const loadConversation = async () => {
      try {
        const data = await api(`/sessions/${selected}`);
        if (!live) return;
        setError((current) => current === connectionError ? '' : current);
        setConversation(data);
        const sessionPath = data.session?.repo_path || data.session?.repository?.root_path;
        const matchingWorkspace = workspaces.find((item) => item.path === sessionPath);
        if (matchingWorkspace) setActiveWorkspace(matchingWorkspace.id);
        else if (sessionPath) setActiveWorkspace('');
        const rows: Row[] = data.tasks || [];
        const running = rows.find((row) => !terminal.has(row.status) && row.status !== 'interrupted');
        const latest = [...rows].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))[0];
        const current = rows.find((row) => row.task_id === data.session?.active_task_id) || running || latest || null;
        setTask(current);
        const nextTaskId = current?.task_id || '';
        if (nextTaskId !== streamTaskId) {
          source?.close();
          streamTaskId = nextTaskId;
          source = nextTaskId ? new EventSource(`/api/tasks/${nextTaskId}/stream`) : null;
          if (source) source.onmessage = () => { loadConversation(); refresh().catch(() => {}); };
        }
      } catch (e) { if (live) setError(e instanceof Error ? e.message : String(e)); }
    };
    loadConversation();
    const timer = window.setInterval(loadConversation, 3000);
    return () => { live = false; window.clearInterval(timer); source?.close(); };
  }, [selected, workspaces]);
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const messageIds = Array.isArray(conversation?.messages)
      ? conversation.messages.map((message: ConversationMessage) => message.message_id)
      : events.filter((event) => event.event_type === 'user.message' || event.event_type === 'assistant.final').map((event) => event.event_id);
    const previous = scrollSnapshotRef.current;
    const sessionChanged = previous.sessionId !== selected;
    const hasNewMessage = messageIds.length > previous.messageIds.length || messageIds.some((id, index) => previous.messageIds[index] !== id);
    scrollSnapshotRef.current = { sessionId: selected, messageIds };
    if (viewTab !== 'conversation' || (!sessionChanged && (!hasNewMessage || !scrollNearBottomRef.current))) return;
    requestAnimationFrame(() => {
      if (!scrollRef.current) return;
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      scrollNearBottomRef.current = true;
    });
  }, [conversation?.messages, events, selected, viewTab]);

  async function action(fn: () => Promise<any>) {
    setBusy(true); setError('');
    try { await fn(); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  function newChat() { setSelected(''); setConversation(null); setTask(null); setPrompt(''); setError(''); }
  function saveWorkspace(event: FormEvent) {
    event.preventDefault();
    const name = draftName.trim(); const path = draftPath.trim();
    if (!name || !path.startsWith('/')) { setError('请填写名称和以 / 开头的本机绝对路径。'); return; }
    const exists = workspaces.find((item) => item.path === path);
    if (exists) {
      const renamed = workspaces.map((item) => item.id === exists.id ? { ...item, name } : item);
      setWorkspaces(renamed); setActiveWorkspace(exists.id);
    } else {
      const next = { id: crypto.randomUUID(), name, path };
      setWorkspaces((items) => [...items, next]); setActiveWorkspace(next.id);
    }
    setDraftName(''); setDraftPath(''); setWorkspaceDialog(false); setError('');
  }
  async function pickWorkspace() {
    await action(async () => {
      const picked = await api('/workspaces/pick', 'POST');
      if (!picked?.path) return;
      setDraftPath(picked.path); setDraftName((value) => value.trim() || picked.name); setError('');
    });
  }
  async function addDroppedFiles(files: FileList | File[]) {
    const selectedFiles = Array.from(files);
    if (!selectedFiles.length) return;
    const next: Attachment[] = [];
    let total = attachments.reduce((sum, item) => sum + item.size, 0);
    for (const file of selectedFiles) {
      if (attachments.length + next.length >= 5) { setError('最多添加 5 个文本文件。'); break; }
      if (file.size > 256 * 1024 || total + file.size > 1024 * 1024) { setError('附件总大小不能超过 1 MB，单个文件不能超过 256 KB。'); continue; }
      try {
        const content = await file.text();
        if (content.includes('\u0000')) { setError(`${file.name} 看起来是二进制文件，目前只支持文本文件。`); continue; }
        next.push({ id: crypto.randomUUID(), name: file.name, size: file.size, content }); total += file.size;
      } catch { setError(`无法读取文件：${file.name}`); }
    }
    if (next.length) { setAttachments((items) => [...items, ...next]); setError(''); }
  }
  function composerDragOver(event: DragEvent<HTMLFormElement>) {
    if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); setDraggingFiles(true); }
  }
  function composerDrop(event: DragEvent<HTMLFormElement>) {
    event.preventDefault(); setDraggingFiles(false); void addDroppedFiles(event.dataTransfer.files);
  }
  async function sendTask(event: FormEvent) {
    event.preventDefault();
    if (sendLockRef.current) return;
    if (!currentWorkspace) { setWorkspaceDialog(true); setError('先设置一个工作区路径，再创建任务。'); return; }
    const userText = prompt.trim();
    if (!userText && !attachments.length) return;
    const text = userText + attachments.map((file) => `${ATTACHMENT_MARKER}${file.name} ---\n${file.content}\n---`).join('');
    sendLockRef.current = true;
    try {
      await action(async () => {
        let sessionId = selected;
        if (!sessionId) {
          const createdSession = await api('/sessions', 'POST', { repo_path: currentWorkspace.path, title: text });
          sessionId = createdSession.session_id || createdSession.session?.session_id;
          if (!sessionId) throw new Error('创建对话成功，但响应中没有 session_id');
        }
        await api(`/sessions/${sessionId}/messages`, 'POST', { content: text, client_message_id: crypto.randomUUID() });
        setPrompt(''); setAttachments([]); setSelected(sessionId);
        setConversation(await api(`/sessions/${sessionId}`));
        await refresh();
      });
    } finally {
      sendLockRef.current = false;
    }
  }
  async function stopTask() {
    if (!task?.task_id || stopPending) return;
    setStopPending(true);
    setError('');
    try {
      const stoppedTask = await api(`/tasks/${task.task_id}/stop`, 'POST');
      if (stoppedTask?.task_id === task.task_id) setTask(stoppedTask);
      if (selected) setConversation(await api(`/sessions/${selected}`));
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStopPending(false);
    }
  }
  function composerKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== 'Enter' || event.shiftKey) return;
    const nativeEvent = event.nativeEvent;
    if (composingRef.current || nativeEvent.isComposing || nativeEvent.keyCode === 229) return;
    // Safari can dispatch the confirming Enter after compositionend with isComposing=false.
    if (compositionEndedAtRef.current && Date.now() - compositionEndedAtRef.current < 50) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }
  async function openUtility(name: string) {
    setUtility(name); setUtilityDetail(null); setUtilityData(null); setUtilityError(''); setError(''); setUtilityLoading(true);
    const paths: Record<string, string> = { 记忆: '/memory', 技能: '/skills', 进化: '/evolution', 配置: '/config' };
    try {
      const data = await api(paths[name]); setUtilityData(data);
      if (name === '配置') { setConfigText(JSON.stringify(data, null, 2)); setSettingsDraft(data); }
    } catch (e) { setUtilityError(e instanceof Error ? e.message : String(e)); }
    finally { setUtilityLoading(false); }
  }
  async function utilityAction(fn: () => Promise<any>) {
    setUtilityLoading(true); setUtilityError('');
    try { await fn(); await refresh(); }
    catch (e) { setUtilityError(e instanceof Error ? e.message : String(e)); }
    finally { setUtilityLoading(false); }
  }

  const finalMessageEvents = events.filter((event) => event.event_type === 'assistant.final');
  const legacyFinalMessages = conversationTasks.flatMap((conversationTask: Row) => {
    // Old tasks have no assistant.final event. Only synthesize their answer once
    // they are terminal; final_answer is also used as a working value between steps.
    if (conversationTask.status !== 'completed' && conversationTask.status !== 'failed') return [];
    const finalText = conversationTask.status === 'completed'
      ? conversationTask.state?.final_answer
      : conversationTask.state?.error ? `任务执行失败：${runtimeErrorMessage(conversationTask.state.error)}` : '';
    if (!finalText || finalMessageEvents.some((event) => event.task_id === conversationTask.task_id)) return [];
    const taskEvents = events.filter((event) => event.task_id === conversationTask.task_id);
    return [{
      event_id: `legacy-final:${conversationTask.task_id}`,
      task_id: conversationTask.task_id,
      seq: Math.max(0, ...taskEvents.map((event) => Number(event.seq) || 0)) + 0.5,
      event_type: 'assistant.final',
      created_at: conversationTask.updated_at || conversationTask.created_at,
      payload: { content: finalText },
    }];
  });
  const legacyMessages = [...events.filter((event) => event.event_type === 'user.message' || event.event_type === 'assistant.final'), ...legacyFinalMessages].sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0)).map((event) => ({
    message_id: event.event_id, role: event.event_type === 'user.message' ? 'user' : 'assistant', kind: event.event_type === 'user.message' ? 'user' : 'final', content: event.payload?.content || '', seq: event.seq, created_at: event.created_at, task_id: event.task_id,
  }));
  const messages: ConversationMessage[] = Array.isArray(conversation?.messages) ? conversation.messages : legacyMessages;
  const traceEvents = task ? events.filter((event) => event.task_id === task.task_id && !['task.updated','context.assembled','context.compacted','user.message.consumed'].includes(event.event_type)) : [];
  const summaryEvents = traceEvents.filter((event) => ['planner.plan','planner.replanned','planner.decision','plan.step.started','plan.step.completed','tool.started','tool.completed','tool.failed','tool.resolved','code.patch','verification.selected','verification.result','verification.repair_requested','task.error','coder.report','assistant.question','node.transition'].includes(event.event_type));
  const latestSummaryEvent = summaryEvents[summaryEvents.length - 1];
  const verificationEvent = [...summaryEvents].reverse().find((event) => event.event_type === 'verification.result');
  const completedSteps = state.plan?.steps?.filter((step: Row) => step.status === 'completed').length || 0;
  const totalSteps = state.plan?.steps?.length || 0;
  const liveSummaryTitle = currentStatus === 'completed' ? '任务已完成'
    : currentStatus === 'failed' ? '任务未完成'
    : currentStatus === 'waiting_user' ? '等待你回复'
    : currentStep?.goal
    ? `正在处理：${currentStep.goal}`
    : currentStatus === 'planning' ? '正在整理任务步骤'
    : currentStatus === 'replanning' ? '正在根据新要求调整步骤'
    : currentStatus === 'repairing' ? '正在根据检查结果修复'
    : state.next_node && !terminal.has(currentStatus) ? `正在处理：${String(state.next_node).toLowerCase().includes('planner') ? 'Planner 规划' : String(state.next_node).toLowerCase().includes('coder') ? 'Coder 执行' : state.next_node}`
    : currentStatus === 'running' ? 'Agent 正在处理任务'
    : latestSummaryEvent ? eventHumanTitle(latestSummaryEvent)
    : labels[currentStatus] || '等待执行记录';
  const liveSummaryDetail = currentStatus === 'completed'
    ? verificationEvent ? eventDetailText(verificationEvent) : totalSteps ? `已完成 ${completedSteps}/${totalSteps} 个步骤。` : '本次任务已完成。'
    : currentStatus === 'failed' && state.error ? runtimeErrorMessage(state.error)
    : latestSummaryEvent
    ? eventDetailText(latestSummaryEvent)
    : state.plan?.goal || '新的执行动作会自动显示在这里。';
  const repoPath = task?.repo_path || state.repo_path || state.task_spec?.repo_path || '';

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">e</span><span>EvoCode</span></div>
      <button className="new-chat" onClick={newChat}><span>＋</span> 新建对话</button>
      <div className="workspace-select-wrap">
        <div className="section-label">工作区 <button className="text-action" onClick={() => setWorkspaceDialog(true)}>管理</button></div>
        <select aria-label="当前工作区" value={activeWorkspace || (sessionWorkspace ? 'session-workspace' : '')} onChange={(e) => { const value = e.target.value; if (value === 'session-workspace') { setActiveWorkspace(''); return; } setActiveWorkspace(value); newChat(); }}>
          <option value="">选择工作区</option>
          {sessionWorkspace && !active && <option value="session-workspace">当前对话 · {sessionWorkspace.name}</option>}
          {workspaces.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
        </select>
        {currentWorkspace && <div className="workspace-path" title={currentWorkspace.path}>{currentWorkspace.path}</div>}
      </div>
      <select className="mobile-session-select" aria-label="切换对话" value={selected} onChange={(event) => { setSelected(event.target.value); setError(''); }}>
        <option value="">新对话</option>{sessions.map((row) => <option key={row.session_id} value={row.session_id}>{sessionTitle(row)}</option>)}
      </select>
      <div className="history-head"><span>对话</span><span className="count">{filteredSessions.length}</span></div>
      <label className="history-search"><span aria-hidden="true">⌕</span><input value={sessionQuery} onChange={(event) => setSessionQuery(event.target.value)} placeholder="搜索对话" aria-label="搜索对话" />{sessionQuery && <button type="button" onClick={() => setSessionQuery('')} aria-label="清除搜索">×</button>}</label>
      <div className="history-list">
        {filteredSessions.length === 0 ? <div className="history-empty"><span>{sessionQuery ? '没有匹配的对话' : active && sessions.length ? '此工作区还没有对话' : '选择工作区后开始新对话'}</span>{!sessionQuery && <button onClick={active ? newChat : () => setWorkspaceDialog(true)}>{active ? '新建对话' : '选择工作区'}</button>}</div> : filteredSessions.map((row) => {
          const sessionTask = row.tasks?.find((item) => item.task_id === row.active_task_id) || row.tasks?.[0];
          const status = sessionTask?.status;
          return <button key={row.session_id} className={`history-item ${selected === row.session_id ? 'chosen' : ''}`} onClick={() => { setSelected(row.session_id); setError(''); }}>
          <span className="history-item-icon" aria-hidden="true">▤</span><span className="history-item-copy"><span className="history-title">{sessionTitle(row)}</span><span className="history-meta">{relativeTime(row.updated_at || row.created_at)}{status && <i className={`history-state state-${status}`}>{labels[status] || status}</i>}</span></span>
        </button>;})}
      </div>
      <div className="sidebar-footer">
        <div className="section-label">工作台</div>
        <div className="utility-links">{['记忆', '技能', '进化', '配置'].map((name) => <button key={name} onClick={() => openUtility(name)} className={utility === name ? 'utility-active' : ''}><span className="utility-icon">{utilityIcons[name]}</span><span className="utility-copy"><b>{utilityLabels[name]}</b><small>{utilityDescriptions[name]}</small></span><span className="utility-arrow">↗</span></button>)}</div>
        <div className="local-note"><span className="local-dot" />本机运行</div>
      </div>
    </aside>

    <main className="main-panel">
      <header className="topbar">
        <div className="topbar-context"><div className="context-heading"><span className="context-name">{selected ? sessionTitle({ ...(conversation?.session || {}), title: conversation?.session?.title || conversation?.title, tasks: conversation?.tasks || [] }) : '新对话'}</span>{repoPath && <span className="context-path" title={repoPath}>{shortPath(repoPath)}</span>}</div>{selected && <nav className="conversation-tabs" aria-label="对话内容"><button className={viewTab === 'conversation' ? 'active' : ''} onClick={() => setViewTab('conversation')}>对话</button><button className={viewTab === 'trace' ? 'active' : ''} onClick={() => setViewTab('trace')}>完整轨迹{task && <span>{events.filter((event) => event.task_id === task.task_id).length}</span>}</button><button className={viewTab === 'graph' ? 'active' : ''} onClick={() => setViewTab('graph')}>节点图</button></nav>}</div>
        {selected && <div className="topbar-actions">
          {currentStatus && <span className={`status-chip status-${currentStatus}`}><i />{labels[currentStatus] || currentStatus}</span>}
          {executionActive && task && <button className="task-stop-button" onClick={() => void stopTask()} disabled={stopPending} aria-label={stopPending ? '正在停止任务' : '停止任务'} title={stopPending ? '正在停止任务' : '停止任务'}>{stopPending ? '停止中…' : <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="5" y="5" width="10" height="10" rx="1" /></svg>}</button>}
          {['interrupted', 'cancelled', 'failed'].includes(currentStatus) && task && <button onClick={() => action(() => api(`/tasks/${task.task_id}/resume`, 'POST'))} disabled={busy}>继续执行</button>}
          <button className="quiet-button" onClick={() => openUtility('进化')} title="从已完成任务整理候选经验">整理经验</button>
        </div>}
      </header>

      {error && <div role="alert" className="toast-error"><span>{error}</span><button onClick={() => setError('')}>关闭</button></div>}

      <div className="conversation-scroll" ref={scrollRef} onScroll={(event) => { const element = event.currentTarget; scrollNearBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 96; }}>
        {!selected ? <div className="welcome-state">
          <div className="welcome-title">想在代码里做什么？</div>
          {!active ? <button className="workspace-nudge" onClick={() => setWorkspaceDialog(true)}>先设置工作区 <span>→</span></button> : <div className="welcome-context"><span className="local-dot" />{active.name}<span className="context-divider">·</span><span title={active.path}>{shortPath(active.path)}</span><button onClick={() => setWorkspaceDialog(true)}>更改</button></div>}
        </div> : <div className={`conversation-content ${viewTab !== 'conversation' ? 'trace-tab-content' : ''}`}>
          {viewTab === 'conversation' ? <>
            {messages.map((message) => {
              const isUser = message.role === 'user' || message.kind === 'user';
              const rawContent = message.content;
              const content = typeof rawContent === 'string' ? rawContent.split(ATTACHMENT_MARKER)[0] : rawContent;
              if (!content) return null;
              const timestamp = Date.parse(message.created_at);
              return <article className={`message ${isUser ? 'user-message' : 'agent-message'} message-${message.kind}`} key={message.message_id} aria-label={isUser ? '你发送的消息' : message.kind === 'question' ? 'Agent 提问' : message.kind === 'progress' ? '执行进度' : 'Agent 回复'}>
                <div className="message-body">{!isUser && message.kind !== 'final' && <div className="message-kind-label">{message.kind === 'question' ? '需要你回复' : '执行进度'}</div>}<div className="message-text">{content}</div>{isUser && typeof rawContent === 'string' && rawContent.includes(ATTACHMENT_MARKER) && <div className="message-attachments">附件：{rawContent.split(ATTACHMENT_MARKER).slice(1).map((block) => block.split(' ---')[0]).filter(Boolean).join('、')}</div>}</div>
                {Number.isFinite(timestamp) && <time className="message-time" dateTime={message.created_at}>{messageTimeFormat.format(timestamp)}</time>}
              </article>;
            })}
            {task && <section className={`execution-summary-live live-${currentStatus}`} aria-live="polite" aria-label="执行摘要">
              <div className="execution-summary-live-indicator" aria-hidden="true" />
              <div className="execution-summary-live-copy"><div className="execution-summary-live-heading"><b>执行摘要</b><span>{labels[currentStatus] || '等待执行记录'}</span></div><strong title={liveSummaryTitle}>{liveSummaryTitle}</strong><p key={latestSummaryEvent?.event_id || currentStatus} title={liveSummaryDetail}>{liveSummaryDetail}</p></div>
              <div className="execution-summary-live-side">{totalSteps > 0 && <span>步骤 {completedSteps}/{totalSteps}</span>}<button onClick={() => setViewTab('trace')}>查看轨迹</button></div>
            </section>}
          </> : task ? <TracePage taskId={task.task_id} mode={viewTab === 'graph' ? 'graph' : 'details'} embedded /> : <div className="trace-empty-state">当前没有可查看的任务轨迹。</div>}
          {task && <div className="task-meta"><span>当前任务</span><span>{relativeTime(task.created_at)}</span><code title={task.task_id}>{task.task_id}</code>{repoPath && <code title={repoPath}>{repoPath}</code>}</div>}
        </div>}
      </div>

      {viewTab === 'conversation' && <div className="composer-zone"><form className={`composer ${draggingFiles ? 'composer-drop-active' : ''}`} onSubmit={sendTask} onDragOver={composerDragOver} onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDraggingFiles(false); }} onDrop={composerDrop}>
        {draggingFiles && <div className="drop-overlay">松开鼠标，将文本文件添加到消息</div>}
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} onCompositionStart={() => { composingRef.current = true; }} onCompositionEnd={() => { composingRef.current = false; compositionEndedAtRef.current = Date.now(); }} onKeyDown={composerKey} placeholder={selected ? '继续这段对话…' : '描述你想完成的代码任务…'} rows={3} aria-label="任务描述" />
        {attachments.length > 0 && <div className="attachment-list">{attachments.map((file) => <div className="attachment-chip" key={file.id}><span className="attachment-icon">▤</span><span className="attachment-copy"><b>{file.name}</b><small>{(file.size / 1024).toFixed(1)} KB · 文本附件</small></span><button type="button" aria-label={`移除 ${file.name}`} onClick={() => setAttachments((items) => items.filter((item) => item.id !== file.id))}>×</button></div>)}</div>}
        <div className="composer-bottom"><div className="composer-tools">
          <label className="tool-pill attach-file-pill" title="选择文本文件">＋ 附件<input type="file" multiple hidden onChange={(event) => { if (event.currentTarget.files) void addDroppedFiles(event.currentTarget.files); event.currentTarget.value = ''; }} /></label>
          <button type="button" className={`workspace-pill ${!currentWorkspace ? 'needs-workspace' : ''}`} onClick={() => setWorkspaceDialog(true)}><span className="folder-icon">⌂</span><span>{currentWorkspace?.name || '选择工作区'}</span><span className="pill-path" title={currentWorkspace?.path}>{currentWorkspace ? shortPath(currentWorkspace.path) : '本机路径'}</span><span className="chevron">⌄</span></button>
        </div><div className="send-side"><span className="enter-hint">Enter 发送 · Shift + Enter 换行</span>{executionActive && task && <button type="button" className="task-stop-button composer-stop-button" onClick={() => void stopTask()} disabled={stopPending} aria-label={stopPending ? '正在停止任务' : '停止任务'} title={stopPending ? '正在停止任务' : '停止任务'}>{stopPending ? '停止中…' : <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="5" y="5" width="10" height="10" rx="1" /></svg>}</button>}<button className="send-button" disabled={busy || !prompt.trim()} title="发送任务">{busy ? <span className="spinner" /> : <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 10h13M10 4l6 6-6 6" /></svg>}</button></div></div>
      </form><div className="composer-footnote">{currentStatus === 'interrupted' ? '已停止，可补充要求后继续。' : '修改代码后，AI 会根据项目选择并运行检查，结果会显示在执行记录中。'}</div></div>}
    </main>

    {workspaceDialog && <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setWorkspaceDialog(false); }}><section className="workspace-modal" role="dialog" aria-modal="true" aria-labelledby="workspace-title">
      <div className="modal-heading"><div><h2 id="workspace-title">工作区</h2><p>为 Agent 指定一个本机仓库目录。</p></div><button className="icon-button" onClick={() => setWorkspaceDialog(false)} aria-label="关闭">×</button></div>
      <div className="saved-workspaces">{workspaces.map((item) => <div className={`saved-workspace ${activeWorkspace === item.id ? 'active-workspace' : ''}`} key={item.id}>
        <button className="saved-workspace-select" onClick={() => { setActiveWorkspace(item.id); setWorkspaceDialog(false); newChat(); }}><span className="workspace-radio">{activeWorkspace === item.id ? '●' : '○'}</span><span className="saved-workspace-copy"><b>{item.name}</b><code title={item.path}>{item.path}</code></span></button><button className="remove-workspace" title={`从列表移除 ${item.name}`} onClick={() => { if (!window.confirm(`从 EvoCode 列表移除“${item.name}”？磁盘上的文件不会被删除。`)) return; setWorkspaces((rows) => rows.filter((row) => row.id !== item.id)); if (activeWorkspace === item.id) setActiveWorkspace(''); }}>移除</button>
      </div>)}{workspaces.length === 0 && <div className="no-workspaces">还没有保存的工作区</div>}</div>
      <form className="workspace-form" onSubmit={saveWorkspace}><div className="form-title">添加工作区</div><button type="button" className="finder-pick-button" onClick={() => void pickWorkspace()}>⌕ <span>从 Finder 选择文件夹</span><b>打开</b></button><div className="path-divider"><span>或粘贴文件夹路径</span></div><label>显示名称<input required value={draftName} onChange={(e) => setDraftName(e.target.value)} placeholder="例如：EvoCode" /></label><label>文件夹路径<input required value={draftPath} onChange={(e) => setDraftPath(e.target.value)} placeholder="选择文件夹后自动填入" /></label><div className="modal-actions"><button type="button" onClick={() => setWorkspaceDialog(false)}>关闭</button><button className="modal-primary" type="submit">保存工作区</button></div></form>
    </section></div>}

    {utility && <div className="utility-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setUtility(''); }}><section className="utility-panel" role="dialog" aria-modal="true" aria-labelledby="utility-title"><div className="utility-top"><div><div className="section-label">工作台</div><h2 id="utility-title">{utilityLabels[utility] || utility}</h2><p>{({记忆:'查找以前任务里的问题和处理记录。',技能:'查看 Agent 保存的经验及适用范围。',进化:'从已完成任务整理新经验，检查后再决定是否启用。',配置:'设置新任务是否查找历史记录、参考经验或自动整理经验。'} as Row)[utility]}</p></div><div className="utility-top-actions"><button className="utility-refresh" onClick={() => void openUtility(utility)} disabled={utilityLoading} title="重新加载">{utilityLoading ? '加载中…' : '刷新'}</button><button className="icon-button" onClick={() => setUtility('')} aria-label="关闭">×</button></div></div>
      {utilityError && <div role="alert" className="utility-error"><span>{utilityError}</span><button onClick={() => setUtilityError('')}>关闭</button></div>}
      {utilityLoading && !utilityData && <div className="utility-loading"><span className="spinner" />正在读取真实数据…</div>}
      {utility === '配置' && settingsDraft && <>
        <div className="utility-section-heading"><b>新任务会使用这些设置</b><span>保存后生效</span></div>
        <div className="setting-list">
          <label className="setting-row"><span><b>搜索相关历史任务</b><small>规划任务时查找相似问题和处理记录。</small></span><input type="checkbox" checked={settingsDraft.raw_rag.enabled} onChange={(e) => setSettingsDraft({...settingsDraft, raw_rag:{...settingsDraft.raw_rag,enabled:e.target.checked}})} /></label>
          <label className="setting-row"><span><b>使用已保存技能</b><small>匹配到相关经验时，将它提供给 Agent 参考。</small></span><input type="checkbox" checked={settingsDraft.skills.enabled} onChange={(e) => setSettingsDraft({...settingsDraft, skills:{...settingsDraft.skills,enabled:e.target.checked}})} /></label>
          <label className="setting-row"><span><b>允许整理新经验</b><small>开启后，可从已完成的任务整理新经验。</small></span><input type="checkbox" checked={settingsDraft.evolution.enabled} onChange={(e) => setSettingsDraft({...settingsDraft, evolution:{...settingsDraft.evolution,enabled:e.target.checked}})} /></label>
          <label className="setting-row"><span><b>任务完成后自动整理</b><small>会调用经验整理模型并可能产生费用；新经验仍需你检查发布。</small></span><input type="checkbox" checked={settingsDraft.evolution.auto_trigger_enabled} onChange={(e) => setSettingsDraft({...settingsDraft, evolution:{...settingsDraft.evolution,auto_trigger_enabled:e.target.checked}})} /></label>
        </div>
        <div className="model-summary"><div><span>编程模型</span><b>{settingsDraft.models?.local?.model_id || '未配置'}</b></div><div><span>经验整理模型</span><b>{settingsDraft.models?.evolution?.model_id || '未配置'}</b></div></div>
        <button className="modal-primary utility-save" disabled={utilityLoading} onClick={() => utilityAction(async () => { const result = await api('/config', 'PUT', settingsDraft); setUtilityData(result); setSettingsDraft(result.config); setConfigText(JSON.stringify(result.config,null,2)); })}>{utilityLoading ? '正在保存…' : '保存设置'}</button>
        <p className="save-note">更改只影响新建的任务；运行中的任务保留创建时的配置。</p>
        <details className="advanced-settings"><summary>高级设置（通常不用修改）</summary><p>这里显示 Agent 的完整配置。修改前请确认内容正确；密钥仍从本机环境变量读取。</p><textarea className="config-editor" value={configText} onChange={(e) => setConfigText(e.target.value)} /><button disabled={utilityLoading} onClick={() => utilityAction(async () => { let config; try { config = JSON.parse(configText); } catch { throw new Error('配置内容格式不正确，请检查后再保存。'); } const result = await api('/config', 'PUT', config); setUtilityData(result); setSettingsDraft(result.config); })}>保存高级设置</button></details>
      </>}
      {utility === '记忆' && utilityData && <>
        <div className="memory-status"><div><b>{utilityData.enabled ? '已开启' : '已关闭'}</b><span>过去任务搜索</span></div><div><b>{utilityData.chunks ?? 0}</b><span>可搜索内容</span></div><div><b>{utilityData.jobs?.pending ?? 0}</b><span>等待整理的任务</span></div></div>
        {!utilityData.embedding_configured && <p className="info-note">向量模型未配置。历史记录仍会保存，检索将使用可用方式降级。</p>}
        <div className="utility-action-card"><div><b>补齐历史任务搜索</b><span>为已保存的任务建立搜索数据。会调用模型，可能产生费用。</span></div><button disabled={utilityLoading} onClick={() => { if (window.confirm('要为已保存的任务补建搜索数据吗？这会调用模型，可能产生费用。')) void utilityAction(async () => setUtilityDetail(await api('/memory/rebuild', 'POST'))); }}>开始整理</button></div>
        <form className="utility-search" onSubmit={(e) => { e.preventDefault(); const query = new FormData(e.currentTarget).get('query'); void utilityAction(async () => setUtilityDetail(await api('/memory/search', 'POST', { task_id: task?.task_id, query }))); }}><label htmlFor="memory-query">搜索过去的任务</label><div><input id="memory-query" name="query" required placeholder="例如：之前怎么修复登录测试？" /><button disabled={!task || utilityLoading}>{utilityLoading ? '正在搜索…' : '搜索'}</button></div>{!task && <small>打开一段对话后，才能按它所属的工作区搜索。</small>}</form>
        {utilityDetail?.evidence && <div className="memory-results"><div className="utility-section-heading"><b>找到 {utilityDetail.evidence.length} 条记录</b><span>{utilityDetail.mode === 'hybrid' ? '混合检索' : utilityDetail.mode || '检索'}</span></div>{utilityDetail.evidence.map((item: Row) => <MemoryEvidence key={item.chunk_id || item.event_id} item={item} />)}</div>}
      </>}
      {utility === '技能' && <>
        <div className="utility-section-heading"><b>已保存的经验</b><span>{Array.isArray(utilityData) ? `${utilityData.length} 项` : ''}</span></div>
        <div className="utility-items">{Array.isArray(utilityData) && utilityData.map((skill: Row) => <div className="utility-item skill-item" key={skill.skill_id}><div className="skill-copy"><b>{skill.name}</b><span>{skill.level === 'general' ? '通用经验' : skill.level === 'meta' ? '经验整理规则' : '当前项目经验'} · {skill.status === 'active' ? '正在使用' : skill.status === 'deprecated' ? '已停用' : '尚未启用'}</span></div><button onClick={() => utilityAction(async () => setUtilityDetail(await api(`/skills/versions?skill_id=${skill.skill_id}`)))}>查看历史版本</button>{skill.status === 'active' && <button className="danger-quiet" onClick={() => { if (window.confirm(`停用“${skill.name}”后，新任务就不会再参考它。要停用吗？`)) void utilityAction(async () => { await api(`/skills/${skill.skill_id}/deprecate`, 'POST'); setUtilityData(await api('/skills')); }); }}>停用</button>}</div>)}</div>
        {Array.isArray(utilityData) && utilityData.length === 0 && <div className="empty-panel"><b>还没有已保存的技能</b><span>完成任务后，可在“学习与改进”里整理经验候选。</span></div>}
      </>}
      {utility === '进化' && <>
        <div className="utility-action-card"><div><b>从当前任务整理经验</b><span>生成待检查的经验候选；会调用模型，可能产生费用。</span></div><button disabled={!task || task.status !== 'completed' || utilityLoading} onClick={() => { if (window.confirm('将调用经验整理模型分析这段任务，可能产生费用。只会生成候选，不会自动发布。要继续吗？')) void utilityAction(async () => { setUtilityDetail(await api('/evolution', 'POST', { task_id: task?.task_id, kind: 'task' })); setUtilityData(await api('/evolution')); }); }}>开始整理</button></div>
        {!task && <p className="info-note">先打开一段已完成的对话，再整理经验。</p>}{task && task.status !== 'completed' && <p className="info-note">当前任务完成后才能整理经验。</p>}
        <div className="utility-section-heading"><b>整理记录</b><span>{Array.isArray(utilityData) ? `${utilityData.length} 项` : ''}</span></div>
        <div className="utility-items">{Array.isArray(utilityData) && utilityData.map((run: Row) => <div className="utility-item evolution-item" key={run.run_id}>
          <button className="evolution-open" onClick={() => utilityAction(async () => setUtilityDetail(await api(`/evolution/${run.run_id}`)))}><b>{run.kind === 'meta' ? '学习策略' : '任务经验'}</b><span>{({candidate:'待检查',needs_review:'需要检查',evaluated:'已评估',published:'已发布',rolled_back:'已撤销合并',rejected:'未通过',skipped:'已跳过'} as Row)[run.status] || run.status}</span></button>
          {run.status === 'evaluated' && run.payload?.evaluation?.passed && <button className="danger-quiet" disabled={utilityLoading} onClick={() => { if (window.confirm('评估已通过，发布后会用于后续任务。确定发布？')) void utilityAction(async () => { setUtilityDetail(await api(`/evolution/${run.run_id}/publish`, 'POST')); setUtilityData(await api('/evolution')); }); }}>发布</button>}
          {run.status === 'published' && run.payload?.evaluation?.promotion?.operation === 'MERGE' && <button className="danger-quiet" disabled={utilityLoading} onClick={() => {
            if (!window.confirm('撤销合并会恢复原有技能版本，并影响后续任务。确定撤销？')) return;
            void utilityAction(async () => {
              try {
                setUtilityDetail(await api(`/evolution/${run.run_id}/rollback`, 'POST'));
              } catch (error) {
                if (error instanceof Error && error.message.startsWith('Merge ') && error.message.includes('changed since publication')) {
                  throw new Error('合并涉及的技能版本或状态已变化，请刷新后检查当前版本。');
                }
                throw error;
              }
              setUtilityData(await api('/evolution'));
            });
          }}>撤销合并</button>}
        </div>)}</div>
        {Array.isArray(utilityData) && utilityData.length === 0 && <div className="empty-panel"><b>还没有整理记录</b><span>打开已完成的任务后，可以从上方生成经验候选。</span></div>}
        <details className="advanced-settings"><summary>进阶：比较两套经验效果</summary><p>在独立副本里运行相同任务，比较不同经验版本的效果。通常无需使用；运行时会调用模型，可能产生费用。提交后可点上方“刷新”查看状态。</p><label>验证任务<textarea value={specText} onChange={(e) => setSpecText(e.target.value)} placeholder='[{"repo_path":"项目路径","prompt":"要验证的任务","acceptance_command":["pytest"]}]' /></label><button disabled={!utilityDetail?.run_id || !specText || utilityLoading} onClick={() => utilityAction(async () => { let task_specs; try { task_specs = JSON.parse(specText); } catch { throw new Error('任务内容格式不对，请检查后再提交。'); } if (!Array.isArray(task_specs) || !task_specs.length) throw new Error('请至少填写一条验证任务。'); if (!window.confirm('将在独立副本中运行验证任务，并可能产生模型费用。要提交评估吗？')) return; const result = await api(`/evolution/${utilityDetail.run_id}/evaluate`, 'POST', { task_specs }); setUtilityDetail({...utilityDetail, evaluation_request: result}); })}>提交验证</button></details>
      </>}
      {utility === '技能' && Array.isArray(utilityDetail) && <div className="utility-detail-block"><div className="utility-section-heading"><b>技能版本</b><span>{utilityDetail.length} 个版本</span></div>{utilityDetail.map((version: Row, index: number) => <article className="version-row" key={version.version_id}><div><b>版本 {utilityDetail.length - index}</b><span>{version.created_at ? relativeTime(version.created_at) : ''} · {version.parent_version_id ? '基于上一版' : '初始版本'}</span></div><button onClick={() => utilityAction(async () => setUtilityDetail(await api(`/skills/versions/${version.version_id}`)))}>查看内容</button></article>)}</div>}
      {utility === '技能' && utilityDetail && !Array.isArray(utilityDetail) && typeof utilityDetail.content === 'string' && <article className="skill-content"><div><b>技能正文</b><span>版本 {String(utilityDetail.version_id || '').slice(0,8)}</span></div><pre>{utilityDetail.content}</pre></article>}
      {utility === '进化' && utilityDetail && !Array.isArray(utilityDetail) && <article className="evolution-result"><b>{({candidate:'候选已生成',needs_review:'需要检查',evaluated:'评估已完成',published:'已发布',rolled_back:'已撤销合并',rejected:'未通过',skipped:'本次已跳过'} as Row)[utilityDetail.status] || '操作已完成'}</b><p>{utilityDetail.reason === 'remote_context_not_authorized' ? '当前策略不允许把远程代码上下文交给模型，因此没有生成候选。你可以检查本机 Evolution 配置后再试。' : utilityDetail.reason || `记录编号：${utilityDetail.run_id || '已提交'}`}</p>{utilityDetail.payload?.rollback && <p>已恢复 {Object.keys(utilityDetail.payload.rollback.restored_source_versions).length} 项源技能。{utilityDetail.payload.rollback.restored_target_version_id ? '目标技能已恢复到合并前的版本。' : '合并生成的新技能已停用。'}</p>}</article>}
      {utilityDetail && !Array.isArray(utilityDetail) && <details className="utility-result"><summary>查看技术详情</summary><pre>{JSON.stringify(utilityDetail, null, 2)}</pre></details>}
      {utilityLoading && utilityData && <div className="utility-inline-loading"><span className="spinner" />正在处理…</div>}
    </section></div>}
  </div>;
}

function Router() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => { const changed = () => setHash(window.location.hash); window.addEventListener('hashchange', changed); return () => window.removeEventListener('hashchange', changed); }, []);
  const traceId = new URLSearchParams(hash.slice(1)).get('trace');
  return traceId ? <TracePage taskId={traceId}/> : <App/>;
}
createRoot(document.getElementById('root')!).render(<Router/>);

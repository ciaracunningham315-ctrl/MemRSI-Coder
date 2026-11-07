import React, {useEffect, useState} from 'react';

type Snapshot = {messages: Record<string, unknown>[]};

const sections: [string, string][] = [
  ['Current task context', '当前任务与有效需求'],
  ['Recent persisted session conversation', '近期会话原文'],
  ['Earlier session summary', '较早会话摘要'],
  ['New user input or execution update', '用户追加消息或执行更新'],
  ['Invalid PlannerDecision', 'Planner 决策校验反馈'],
  ['Untrusted retrieved evidence', '检索证据'],
  ['Applicable versioned skills', '已加载技能'],
  ['Prior ', '先前执行摘要'],
  ['Compacted ', '压缩后的执行摘要'],
  ['Coder tool history coverage', '工具历史覆盖说明'],
  ['Planner tool history coverage', '工具历史覆盖说明'],
];
const roles: Record<string, string> = {system:'系统', user:'输入', assistant:'模型', tool:'工具结果'};

function parseStructuredText(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try {
    const parsed = JSON.parse(value);
    if (parsed !== null && typeof parsed === 'object') return parsed;
  } catch { /* Ordinary text is shown as recorded. */ }
  return value;
}

function SnapshotValue({value}: {value: unknown}) {
  if (Array.isArray(value) && value.length) {
    return <ol className="snapshot-values">{value.map((item, index) => <li key={index}><SnapshotValue value={item}/></li>)}</ol>;
  }
  if (value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length) {
    return <dl className="snapshot-properties">{Object.entries(value).map(([name, entry]) => <div key={name}>
      <dt>{name}</dt><dd><SnapshotValue value={name === 'arguments' ? parseStructuredText(entry) : entry}/></dd>
    </div>)}</dl>;
  }
  return <pre>{typeof value === 'string' ? value : JSON.stringify(value)}</pre>;
}

function SnapshotMessageContent({content, section}: {content: unknown; section?: [string, string]}) {
  const line = section && typeof content === 'string' ? content.indexOf('\n') : -1;
  if (typeof content === 'string' && line >= 0) {
    return <><pre>{content.slice(0, line)}</pre><SnapshotValue value={parseStructuredText(content.slice(line + 1))}/></>;
  }
  return <SnapshotValue value={parseStructuredText(content)}/>;
}

export default function ContextSnapshot({artifactId}: {artifactId: string}) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch('/api/artifacts/' + encodeURIComponent(artifactId), {signal:controller.signal});
        if (!response.ok) throw new Error('读取失败（HTTP ' + response.status + '）');
        const data: Snapshot = await response.json();
        if (!Array.isArray(data?.messages) || !data.messages.every(item => item && typeof item === 'object')) {
          throw new Error('这份快照缺少可读的消息列表');
        }
        if (!controller.signal.aborted) setSnapshot(data);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
      }
    }
    void load();
    return () => controller.abort();
  }, [artifactId, attempt]);

  if (error) return <p className="snapshot-status" role="alert">{error} <button type="button" onClick={() => {setError(''); setAttempt(value => value + 1);}}>重试</button></p>;
  if (!snapshot) return <p className="snapshot-status" role="status">正在读取上下文快照…</p>;

  return <section className="context-snapshot" aria-label="上下文原文">
    <h4>本次输入原文 · {snapshot.messages.length} 条</h4>
    <p className="snapshot-status">按快照中的实际输入顺序展示。角色为输入的条目也可能由系统组装。</p>
    <ol className="snapshot-messages">
      {snapshot.messages.map(({role, content, ...fields}, index) => {
        const section = typeof content === 'string' ? sections.find(([prefix]) => content.startsWith(prefix)) : undefined;
        const title = section?.[1] ?? (role === 'system' ? '系统指令' : role === 'tool' ? '工具结果' : fields.tool_calls ? '工具调用' : '消息内容');
        return <li key={index}>
          <div className="snapshot-message-heading"><b>{index + 1}. {title}</b><span>{roles[String(role)] ?? String(role)}</span></div>
          {content != null && content !== '' && <SnapshotMessageContent content={content} section={section}/>}
          {Object.entries(fields).map(([key, value]) => <div key={key} className="snapshot-field">
            <span>{key === 'tool_calls' ? '调用参数' : key === 'tool_call_id' ? '对应调用编号' : key}</span>
            <SnapshotValue value={value}/>
          </div>)}
          {(content == null || content === '') && Object.keys(fields).length === 0 && <p className="snapshot-status">空消息</p>}
        </li>;
      })}
    </ol>
    <details className="timeline-raw"><summary>查看快照原始 JSON</summary><pre>{JSON.stringify(snapshot, null, 2)}</pre></details>
  </section>;
}

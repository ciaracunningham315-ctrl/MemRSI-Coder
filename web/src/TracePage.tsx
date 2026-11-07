import React, {useEffect,useMemo,useRef,useState} from 'react';
import {buildStages,decisionSummary,eventTitle,reportSummary,stageNames,StageKind,TraceEvent,TraceStage} from './trace-model';
import ContextSnapshot from './ContextSnapshot';
import './trace.css';
type Row=Record<string,any>;
const statusLabels:Record<string,string>={completed:'完成',failed:'失败',passed:'通过',blocked:'环境阻塞，未验证',unknown:'未验证',recorded:'已记录',unresolved:'等待结果',running:'执行中',waiting_user:'等待用户回复',cancelled:'已取消',needs_review:'等待核对',planning:'规划中',replanning:'重新规划',repairing:'修复中',ok:'正常',degraded:'降级检索',memory_disabled:'历史检索已关闭'};
async function get(path:string){const r=await fetch('/api'+path);const data=await r.json();if(!r.ok)throw new Error(data.detail||'加载失败');return data;}
function time(value?:string){return value?new Date(value).toLocaleTimeString():'';}
function text(value:any){return typeof value==='string'?value:value==null?'':JSON.stringify(value,null,2);}
function toolLabel(tool?:string){return ({read_file:'读取文件',edit_file:'修改文件',run_command:'运行命令',run_tests:'运行检查',memory_search:'搜索历史'} as Record<string,string>)[tool||'']||tool||'工具操作';}
function eventBrief(e:TraceEvent){
  const p=e.payload||{},a=p.arguments||{};
  if(e.event_type==='verification.selected')return (p.checks||[]).map((check:Row)=>`${check.name}：${check.command?.join(' ')||''}`).join('；')||'没有找到适合项目的检查配置';
  if(e.event_type==='planner.tool.started')return 'Planner 读取本会话已保存的消息原文';
  if(e.event_type==='planner.tool.completed')return p.result?.error||p.result?.reason||`${p.result?.cached?'复用':'读取'} ${p.result?.messages?.length||0} 条消息${p.result?.has_more?'，还有后续记录':''}`;
  if(e.event_type==='coder.report.only_requested')return `跳过巡检，下一轮只提交报告${p.tools?.length?` · ${p.tools.join('、')}`:''}`;
  if(e.event_type==='user.message')return p.content||'收到用户消息';
  if(e.event_type==='planner.plan'||e.event_type==='planner.replanned')return p.plan?.goal||p.reason||'已整理任务计划';
  if(e.event_type==='planner.decision')return decisionSummary(p);
  if(e.event_type==='coder.message')return p.message?.content||`Agent 准备调用 ${p.message?.tool_calls?.length||0} 项工具`;
  if(e.event_type==='coder.report')return reportSummary(p);
  if(e.event_type==='assistant.question')return p.content||p.question||p.summary||'Agent 正在等待你回复';
  if(e.event_type==='node.transition')return `${text(p.from_node||p.from)||'当前节点'} → ${text(p.to_node||p.to)||'下一节点'}`;
  if(e.event_type==='tool.started')return p.tool==='run_command'?(Array.isArray(a.command)?a.command.join(' '):a.command||'正在运行命令'):`${toolLabel(p.tool)}${a.path?`：${a.path}`:''}`;
  if(e.event_type==='tool.completed'||e.event_type==='tool.resolved')return text(p.output||p.result)||'操作已完成';
  if(e.event_type==='tool.failed'||e.event_type==='task.error')return text(p.error||p.message||p.output)||'操作失败';
  if(e.event_type==='verification.result')return p.coverage==='syntax_only'?(p.status==='passed'?'语法检查通过，未进行功能测试':'语法检查未通过，尚未验证功能'):text(p.output||p.reason)||`检查${p.status==='passed'?'通过':p.status==='failed'?'未通过':'尚未完成'}`;
  if(e.event_type==='plan.step.started'||e.event_type==='plan.step.completed')return p.goal||p.result||'';
  if(e.event_type==='memory.retrieved')return `“${p.query||'任务相关经验'}” · 找到 ${p.evidence_event_ids?.length||0} 条历史证据`;
  if(e.event_type==='skill.loaded')return `${p.skill_id||'经验技能'} · 版本 ${p.version_id||'未知'}`;
  if(e.event_type==='context.assembled'){const role=String(p.role||p.node||'').toLowerCase().includes('planner')?'Planner':'Coder';const snapshot=p.snapshot_id||p.context_id||p.artifact_id||p.snapshot_ref;return `${role} 收到 ${p.message_count??'—'} 条消息、${p.evidence_count??0} 条历史证据、${p.skill_count??0} 项技能${snapshot?` · 快照 ${text(snapshot)}`:''}`;}
  if(e.event_type==='context.compacted')return p.summary||'整理了较早的执行记录';
  if(e.event_type==='model.usage')return p.input_tokens==null?'模型未返回输入 Token':`本次调用输入 ${p.input_tokens.toLocaleString()} Token`;
  if(e.event_type==='code.patch')return (p.file_paths||[]).join('、')||'记录了代码修改';
  if(e.event_type==='verification.repair_requested')return p.feedback||'检查未通过，Agent 根据反馈继续修复';
  if(e.event_type==='task.updated')return `任务状态：${statusLabels[p.status]||p.status||'已更新'}`;
  return text(p.summary||p.content||p.result);
}
function eventBody(e:TraceEvent, showSnapshot=true){
  const p=e.payload||{},a=p.arguments||{};
  if(e.event_type==='verification.selected'||e.event_type==='verification.result')return <>{p.reason&&<p className="timeline-paragraph">{text(p.reason)}</p>}{p.coverage==='syntax_only'&&<p className="timeline-muted">本次仅检查了语法，不能据此确认功能正确。</p>}{(p.checks||[]).map((item:Row,index:number)=>{const check=item.check||item;return <section key={check.id||index}><div className="timeline-facts"><span>检查</span><b>{check.name}</b><span>工作目录</span><code>{check.cwd||'.'}</code><span>命令</span><code>{check.command?.join(' ')}</code><span>来源</span><code>{check.source}</code>{item.status&&<><span>结果</span><b>{statusLabels[item.status]||item.status}</b><span>退出码</span><code>{item.result?.exit_code??'未执行完成'}</code></>}</div>{item.result?.output&&<pre className="timeline-output">{item.result.output}</pre>}</section>})}{!p.checks?.length&&p.result?.output&&<pre className="timeline-output">{p.result.output}</pre>}</>;
  if(e.event_type==='planner.tool.completed')return <><p className="timeline-paragraph">{eventBrief(e)}</p>{(p.result?.messages||[]).map((message:Row)=><div key={message.message_id} className="timeline-paragraph"><b>{message.role==='user'?'用户消息':'Agent 消息'} · #{message.seq}</b><p className="trace-message-copy">{text(message.content)}</p></div>)}{p.result?.has_more===true&&<p className="timeline-muted">下次读取从 #{p.result.next_after_seq} 之后继续。</p>}{p.result?.has_more===false&&<p className="timeline-muted">已到本次查询的末尾。</p>}</>;
  if(e.event_type==='planner.tool.started')return <><p className="timeline-paragraph">读取当前会话已保存的消息。</p><div className="timeline-facts"><span>工具</span><code>{text(p.tool)}</code><span>读取范围</span><b>从 #{a.after_seq ?? 0} 之后{a.before_seq != null ? `到 #${a.before_seq} 之前` : ''}，最多 {a.limit ?? 50} 条</b></div></>;
  if(e.event_type==='tool.started')return <><div className="timeline-facts"><span>操作</span><b>{toolLabel(p.tool)}</b>{a.path&&<><span>文件</span><code>{a.path}</code></>}{p.tool==='run_command'&&<><span>命令</span><pre>{Array.isArray(a.command)?a.command.join(' '):text(a.command)}</pre></>}{p.prepared?.before_hash&&<><span>修改前</span><code>{p.prepared.before_hash}</code></>}</div></>;
  if(['tool.completed','tool.failed','tool.resolved','tool.interrupted'].includes(e.event_type))return <><div className="timeline-facts"><span>结果</span><b>{statusLabels[p.status]||statusLabels[p.resolution]|| (e.event_type==='tool.failed'?'失败':'完成')}</b>{typeof p.exit_code==='number'&&<><span>退出码</span><code>{p.exit_code}</code></>}{p.file_paths?.length>0&&<><span>涉及文件</span><code>{p.file_paths.join('、')}</code></>}{p.git_commit&&<><span>代码版本</span><code>{p.git_commit.slice(0,12)}</code></>}</div>{(p.output||p.error||typeof p.exit_code==='string')&&<pre className="timeline-output">{p.output||p.error||p.exit_code}</pre>}</>;
  if(e.event_type==='planner.plan'||e.event_type==='planner.replanned'){const plan=p.plan||{};return <><p className="timeline-paragraph">{plan.goal||p.reason}</p>{plan.steps?.length>0&&<ol className="timeline-steps">{plan.steps.map((s:Row,i:number)=><li key={s.id||i}><span>{statusLabels[s.status]||'待执行'}</span><b>{s.goal}</b>{s.done_when&&<small>完成条件：{s.done_when}</small>}</li>)}</ol>}{plan.constraints?.length>0&&<p className="timeline-muted">限制：{plan.constraints.join('；')}</p>}</>}
  if(e.event_type==='coder.message'){const calls=p.message?.tool_calls||[];return <>{p.message?.content&&<p className="timeline-paragraph">{p.message.content}</p>}{calls.length>0&&<div className="timeline-tool-intents"><b>准备调用 {calls.length} 项工具</b>{calls.map((c:Row,i:number)=><div key={c.id||i}><span>{toolLabel(c.function?.name||c.name)}</span><code>{text(c.function?.arguments||c.arguments)}</code></div>)}</div>}{p.usage&&<p className="timeline-muted">本次模型用量：{p.usage.total_tokens??'未返回'} Token</p>}</>}
  if(e.event_type==='planner.decision'){
    const d=p.decision&&typeof p.decision==='object'?p.decision:{};const assignment=d.assignment&&typeof d.assignment==='object'?d.assignment:{};const plan=d.plan&&typeof d.plan==='object'?d.plan:{};
    return <><p className="timeline-paragraph">{decisionSummary(p)}</p><div className="timeline-facts">{d.action&&<><span>决策动作</span><b>{text(d.action)}</b></>}{d.question&&<><span>向用户提问</span><b>{text(d.question)}</b></>}{d.answer&&<><span>回答内容</span><b>{text(d.answer)}</b></>}{Object.keys(assignment).length>0&&<><span>任务分配</span><b>{text(assignment.summary||assignment.goal||assignment.task||assignment.prompt||assignment)}</b></>}{Object.keys(plan).length>0&&<><span>执行计划</span><b>{text(plan.goal||plan.summary||`${Array.isArray(plan.steps)?plan.steps.length:0} 个步骤`)}</b></>}{p.reason&&<><span>决策原因</span><b>{text(p.reason)}</b></>}{p.usage?.total_tokens!=null&&<><span>模型用量</span><b>{p.usage.total_tokens} Token</b></>}</div></>;
  }
  if(e.event_type==='coder.report'){
    const r=p.report&&typeof p.report==='object'?p.report:{};const checks=Array.isArray(r.checks)?r.checks:[];const refs=Array.isArray(r.artifact_refs)?r.artifact_refs:[];
    return <><p className="timeline-paragraph">{reportSummary(p)}</p><div className="timeline-facts">{r.step_id&&<><span>步骤</span><b>{text(r.step_id)}</b></>}{r.outcome&&<><span>结果</span><b>{text(r.outcome)}</b></>}{Array.isArray(r.changed_files)&&r.changed_files.length>0&&<><span>修改文件</span><b>{r.changed_files.map(text).join('、')}</b></>}{checks.length>0&&<><span>检查</span><b>{checks.map((check:any)=>typeof check==='string'?check:text(check?.name||check?.command||check?.status||check)).join('；')}</b></>}{refs.length>0&&<><span>产物引用</span><code>{refs.map(text).join('、')}</code></>}{p.usage?.total_tokens!=null&&<><span>模型用量</span><b>{p.usage.total_tokens} Token</b></>}</div></>;
  }
  if(e.event_type==='assistant.question')return <><p className="timeline-paragraph">{text(p.content||p.question||p.summary||'Agent 需要你补充信息。')}</p>{(p.status||p.waiting_for)&&<div className="timeline-facts">{p.status&&<><span>状态</span><b>{statusLabels[p.status]||p.status}</b></>}{p.waiting_for&&<><span>等待内容</span><b>{text(p.waiting_for)}</b></>}</div>}</>;
  if(e.event_type==='node.transition')return <div className="timeline-facts"><span>来源节点</span><b>{text(p.from_node||p.from)||'未记录'}</b><span>目标节点</span><b>{text(p.to_node||p.to)||'未记录'}</b>{p.reason&&<><span>原因</span><b>{text(p.reason)}</b></>}</div>;
  if(e.event_type==='user.message')return <p className="timeline-paragraph">{p.content}</p>;
  if(e.event_type==='verification.result')return <><div className={`timeline-verdict verdict-${p.status||'unknown'}`}>{p.status==='passed'?'检查通过':p.status==='failed'?'检查未通过':'检查结果'}{p.command&&<code>{p.command}</code>}</div>{p.reason&&<p className="timeline-paragraph">{p.reason}</p>}{p.output&&<pre className="timeline-output">{p.output}</pre>}</>;
  if(e.event_type==='verification.repair_requested'||e.event_type==='task.error')return <p className="timeline-error-copy">{p.feedback||p.message||p.error}</p>;
  if(e.event_type==='context.assembled'){
    const role=String(p.role||p.node||'').toLowerCase().includes('planner')?'Planner 规划器':'Coder 编码器';
    const artifactId=p.artifact_id||p.node_context?.artifact_id;
    const unit=p.input_size_unit==='estimated_token_units'?'估算 Token':p.input_size_unit==='bytes'?'字节':'单位未记录';
    return <><div className="timeline-facts">
      <span>角色</span><b>{role}</b>
      <span>输入消息</span><b>{p.message_count??'—'} 条</b>
      <span>历史证据</span><b>{p.evidence_count??0} 条</b>
      <span>技能</span><b>{p.skill_count??0} 项</b>
      <span>输入大小</span><b>{typeof p.input_tokens_exact==='number'
        ? `${p.input_tokens_exact.toLocaleString()} Token（发送前精确计数）`
        : p.input_size_estimate!=null ? `${p.input_size_estimate.toLocaleString()} ${unit}` : '未记录'}</b>
      {typeof p.output_token_reserve==='number'&&<><span>预留输出</span><b>{p.output_token_reserve.toLocaleString()} Token</b></>}
      {p.session_revision!=null&&<><span>会话版本</span><b>#{p.session_revision}</b></>}
    </div>{showSnapshot&&(typeof artifactId==='string'&&artifactId
      ? <ContextSnapshot key={artifactId} artifactId={artifactId}/>
      : <p className="snapshot-status">此事件没有保存可读取的上下文快照。</p>)}</>;
  }
  if(e.event_type==='memory.retrieved')return <div className="timeline-facts"><span>检索内容</span><b>{p.query||'任务相关经验'}</b><span>检索方式</span><b>{p.mode||'未记录'} · {statusLabels[p.status]||p.status||'已完成'}</b><span>匹配记录</span><b>{p.evidence_event_ids?.length||0} 条</b>{p.evidence_event_ids?.length>0&&<><span>记录编号</span><code>{p.evidence_event_ids.join('、')}</code></>}</div>;
  if(e.event_type==='skill.loaded')return <div className="timeline-facts"><span>技能</span><b>{p.skill_id||'未记录'}</b><span>固定版本</span><code>{p.version_id||'未记录'}</code></div>;
  if(e.event_type==='plan.step.started'||e.event_type==='plan.step.completed')return <div className="timeline-facts"><span>步骤</span><b>{p.goal||p.step_id||'未记录'}</b>{p.attempt&&<><span>尝试次数</span><b>{p.attempt}</b></>}{p.result&&<><span>完成说明</span><b>{p.result}</b></>}</div>;
  if(e.event_type==='code.patch')return <><div className="timeline-facts"><span>修改文件</span><b>{p.file_paths?.join('、')||'未记录'}</b></div>{p.patch&&<details className="timeline-patch"><summary>查看代码差异</summary><pre>{p.patch}</pre></details>}</>;
  if(e.event_type==='context.compacted')return <p className="timeline-paragraph">{p.summary||'整理了较早的执行记录以继续任务。'}</p>;
  return <p className="timeline-paragraph">{eventBrief(e)||'此记录没有附带更多说明。'}</p>;
}
function TimelineStage({stage,index}:{stage:TraceStage;index:number}){
  const [open,setOpen]=useState(false);
  const first=stage.events[0],failed=stage.status==='failed'||stage.events.some(e=>['tool.failed','task.error'].includes(e.event_type));
  const elapsed=stage.events.length>1?Math.max(0,(new Date(stage.events[stage.events.length-1].created_at).getTime()-new Date(first.created_at).getTime())/1000):null;
  return <details className={`trace-timeline-item ${failed?'is-failed':''}`} onToggle={event=>{if(event.target===event.currentTarget)setOpen(event.currentTarget.open);}}>
    <summary><span className="timeline-index">{index+1}</span><span className="timeline-marker"/><span className="timeline-headline"><b>{stage.title.replace(/^(Planner|Coder) · /,'')}</b><small>{eventBrief(first)||stage.subtitle||stageNames[stage.kind]}</small></span><span className={`timeline-status ${failed?'status-bad':''}`}>{failed?'失败':statusLabels[stage.status]||'已记录'}</span>{elapsed!==null&&<span className="timeline-duration">{elapsed.toFixed(1)} 秒</span>}<time>{time(first?.created_at)}</time><span className="timeline-chevron">⌄</span></summary>
    <div className="trace-timeline-body">{stage.events.map((e:TraceEvent)=><section className="timeline-event-detail" key={e.event_id}><header><b>{eventTitle(e)}</b><span>记录 #{e.seq}</span><time>{time(e.created_at)}</time></header>{eventBody(e,open)}<details className="timeline-raw"><summary>查看原始记录</summary><pre>{JSON.stringify(e,null,2)}</pre></details></section>)}</div>
  </details>;
}
function Detail({stage}:{stage:TraceStage|null}){
  if(!stage)return <p className="trace-empty">选择图上的节点或下方记录，查看它的实际输入和结果。</p>;
  return <><div className="trace-detail-title"><span>{stageNames[stage.kind]}</span><h3>{stage.title}</h3><small>事件序号 {stage.events.map(e=>e.seq).join('、')}</small></div>
    {stage.events.map(e=><div className="trace-content" key={e.event_id}><h4>{eventTitle(e)} <time>{time(e.created_at)}</time></h4>{eventBody(e)}<details className="timeline-raw"><summary>查看原始记录</summary><pre>{JSON.stringify(e,null,2)}</pre></details></div>)}</>;
}
function DetailDrawer({stage,onClose}:{stage:TraceStage|null;onClose:()=>void}){
  const ref=useRef<HTMLDialogElement>(null);
  useEffect(()=>{
    const dialog=ref.current;
    dialog?.showModal();
    return ()=>dialog?.close();
  },[]);
  return <dialog ref={ref} className="trace-detail-drawer" aria-label="轨迹节点详情"
    onCancel={event=>{event.preventDefault();onClose();}}
    onClick={event=>{if(event.target===event.currentTarget)onClose();}}>
    <header><b>节点详情</b><button type="button" onClick={onClose} aria-label="关闭节点详情">×</button></header>
    <div className="trace-detail" key={stage?.id}><Detail stage={stage}/></div>
  </dialog>;
}
function PathGraph({stages,selected,onSelect}:{stages:TraceStage[];selected:string;onSelect:(id:string)=>void}){
  const [zoom,setZoom]=useState(1);
  const lanes:Record<StageKind,number>={user:45,planner:150,coder:255,tool:360,memory:360,skill:360,context:360,verification:360,waiting:360,end:255};
  const gap=238,nodeWidth=198,nodeHeight=96,width=Math.max(760,stages.length*gap+20),height=490;
  const clip=(value:string,max:number)=>value.length>max?value.slice(0,max-1)+'…':value;
  return <><div className="trace-graph-toolbar"><span>每个方框对应实际记录阶段。点击后可查看完整输入、工具参数、结果和原始事件。</span><div><button onClick={()=>setZoom(z=>Math.max(.5,z-.25))} aria-label="缩小轨迹图">−</button><span>{Math.round(zoom*100)}%</span><button onClick={()=>setZoom(z=>Math.min(1.5,z+.25))} aria-label="放大轨迹图">＋</button></div></div><div className="trace-graph-scroll"><svg width={width*zoom} height={height*zoom} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="本任务实际事件流转图"><defs><marker id="trace-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10" fill="#9298a1"/></marker></defs>
    {[45,150,255,360].map(y=><line key={y} x1="0" y1={y+nodeHeight/2} x2={width} y2={y+nodeHeight/2} stroke="#e9e9e8"/>)}
    {stages.slice(1).map((s,i)=>{const a=stages[i];const x=i*gap+20+nodeWidth,y=lanes[a.kind]+nodeHeight/2,nx=(i+1)*gap+20,ny=lanes[s.kind]+nodeHeight/2;return <path key={s.id} d={`M${x} ${y} C${x+20} ${y},${nx-20} ${ny},${nx} ${ny}`} fill="none" stroke="#9298a1" strokeWidth="1.5" markerEnd="url(#trace-arrow)"/>;})}
    {stages.map((s,i)=>{
      const first=s.events[0],last=s.events[s.events.length-1],stamp=time(first?.created_at);
      const detail=s.inputLabel||s.toolName;
      return <g key={s.id} transform={`translate(${i*gap+20},${lanes[s.kind]})`} role="button" tabIndex={0}
        aria-label={`${i+1}. ${s.title}，${s.events.length} 条记录，${statusLabels[s.status]||s.status}${detail?`，${detail}`:''}`}
        onClick={()=>onSelect(s.id)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onSelect(s.id);}}}
        className={`trace-svg-node kind-${s.kind} ${selected===s.id?'is-selected':''} ${s.status==='failed'?'is-failed':''}`}>
        <rect width={nodeWidth} height={nodeHeight} rx="10"/>
        <text x="12" y="19" className="trace-node-index">{i+1} · {stageNames[s.kind]} · {statusLabels[s.status]||s.status}</text>
        <text x="12" y="41">{clip(s.title,22)}</text>
        {detail&&<text x="12" y="61" className="trace-node-detail">{detail}</text>}
        <text x="12" y={detail?81:67} className="trace-node-meta">{s.events.length} 条事件 · #{first?.seq}{last?.seq!==first?.seq?`–${last?.seq}`:''} · {stamp}</text>
        <title>{`${s.title}\n${detail?detail+'\n':''}${s.subtitle}\n${s.events.length} 条记录 · ${statusLabels[s.status]||s.status}`}</title>
      </g>;
    })}
  </svg></div></>;
}
function Architecture({stages,onFilter}:{stages:TraceStage[];onFilter:(kind:StageKind|'all')=>void}){
  const count=(kind:StageKind)=>stages.filter(s=>s.kind===kind).length;
  const reportCount=stages.filter(s=>s.events.some(e=>e.event_type==='coder.report')).length;
  const nodes:[string,StageKind,number,number,string,number?][]=[
    ['user','user',20,150,'用户消息'],['planner','planner',220,150,'Planner'],['coder','coder',420,150,'Coder'],['tool','tool',640,150,'工具'],
    ['memory','memory',220,25,'历史检索'],['context','context',420,25,'上下文组装'],['skill','skill',640,25,'经验技能'],
    ['verification','verification',20,285,'项目检查'],['waiting','waiting',220,285,'等待用户回复'],['coder-report','coder',420,285,'Coder 汇报',reportCount],['end','end',640,285,'END'],
  ];
  return <><p className="trace-graph-note">箭头展示 Planner、Coder 和工具之间的执行路径，包括 Coder 汇报回到 Planner，以及 Planner 结束任务或等待用户回复的分支。</p><div className="trace-graph-scroll"><svg width="830" height="390" viewBox="0 0 830 390" role="img" aria-label="Planner Coder 执行路径与结束分支"><defs><marker id="arch-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10" fill="#a2a7af"/></marker></defs><g fill="none" stroke="#a2a7af" strokeWidth="1.4" markerEnd="url(#arch-arrow)">
    <path d="M180 180H220"/><path d="M380 180H420"/><path d="M580 165H640"/><path d="M640 195H580"/>
    <path d="M300 210V285"/><path d="M380 165C460 105 610 110 640 285"/>
    <path d="M500 210V285"/><path d="M420 315H400V255H300V210"/>
    <path d="M500 85V145"/><path d="M380 55H420"/><path d="M640 85V145"/>
    <path d="M180 315H400V195H420" strokeDasharray="5 4"/>
  </g>
    {nodes.map(([id,kind,x,y,title,overrideCount])=>{const actualCount=overrideCount??count(kind);return <g key={id} transform={`translate(${x},${y})`} role="button" tabIndex={0} onClick={()=>onFilter(kind)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onFilter(kind);}}} className={`trace-svg-node ${actualCount?'':'arch-unrecorded'}`} aria-label={`${title}，记录 ${actualCount} 个阶段`}><rect width="160" height="60" rx="10"/><text x="12" y="24">{title}</text><text x="12" y="45" className="trace-node-index">{actualCount?`记录 ${actualCount} 个阶段`:'没有对应事件记录'}</text></g>;})}
    <text x="20" y="375" fill="#7e887f" fontSize="12">实线表示节点流转，虚线表示项目检查失败后的修复回路；计数来自当前任务记录。</text>
  </svg></div></>;
}
export default function TracePage({taskId,mode:displayMode='standalone',embedded=false}:{taskId:string;mode?:'standalone'|'details'|'graph';embedded?:boolean}){
  const [task,setTask]=useState<Row|null>(null),[events,setEvents]=useState<TraceEvent[]>([]),[error,setError]=useState(''),[selected,setSelected]=useState(''),[mode,setMode]=useState('path'),[filter,setFilter]=useState<StageKind|'all'>('all'),[detailOpen,setDetailOpen]=useState(false);
  useEffect(()=>{let live=true;const load=async()=>{try{const [t,e]=await Promise.all([get('/tasks/'+encodeURIComponent(taskId)),get('/tasks/'+encodeURIComponent(taskId)+'/events')]);if(live){setTask(t);setEvents(e);setError('');}}catch(e){if(live)setError(String(e));}};load();const timer=window.setInterval(load,3000);return()=>{live=false;clearInterval(timer);};},[taskId]);
  const stages=useMemo(()=>buildStages(events,taskId),[events,taskId]);
  const visible=stages.filter(s=>filter==='all'||s.kind===filter);
  const chosen=stages.find(s=>s.id===selected)||[...stages].reverse().find(s=>s.kind==='tool')||stages[stages.length-1]||null;
  const state=task?.state||{};
  const taskEvents=events.filter(e=>e.task_id===taskId).sort((a,b)=>a.seq-b.seq);
  const toolCalls=taskEvents.filter(e=>['tool.started','planner.tool.started'].includes(e.event_type)).length;
  const modelTurns=state.model_responses??taskEvents.filter(e=>['planner.decision','planner.plan','planner.replanned','coder.message','context.compacted'].includes(e.event_type)).length;
  const tokenUsage=Number(state.tokens??taskEvents.reduce((sum,e)=>sum+(Number(e.payload?.usage?.total_tokens)||0),0));
  const startTime=taskEvents[0]?.created_at?new Date(taskEvents[0].created_at).getTime():0;
  const endTime=taskEvents[taskEvents.length-1]?.created_at?new Date(taskEvents[taskEvents.length-1].created_at).getTime():0;
  const duration=state.duration_seconds!==undefined?Math.max(0,Number(state.duration_seconds)):startTime&&endTime?Math.max(0,Math.round((endTime-startTime)/1000)):null;
  return <div className={`trace-page ${embedded?'trace-embedded':''}`}>
    {!embedded&&<header className="trace-header"><a className="trace-back" href={task?'#session='+task.session_id:'#'}>← 返回对话</a><div><b>执行轨迹</b><span>{task?.prompt||'加载任务…'}</span></div><button onClick={()=>window.open(window.location.href,'_blank','noopener')}>新窗口 ↗</button></header>}
    {error&&<div role="alert" className="trace-error">{error}</div>}
    <div className="trace-info"><span className="trace-task-status">{statusLabels[task?.status]||task?.status||'加载中'}</span><span>{toolCalls} 次工具调用</span><span>{modelTurns} 次模型回复</span><span>{tokenUsage?`${tokenUsage.toLocaleString()} Token`:'Token 用量未返回'}</span><span>{duration===null?'耗时未记录':`${state.duration_seconds!==undefined?'任务耗时':'事件跨度'} ${duration.toFixed(1)} 秒`}</span></div>
    {displayMode==='details'&&embedded?<main className="trace-full-events"><div className="trace-full-heading"><div><h2>完整轨迹</h2><p>按任务实际发生顺序，还原规划、模型回复、工具执行和检查结果。展开任一步查看具体输入、输出与原始记录。</p></div><span>{stages.length} 个动作 · {taskEvents.length} 条底层记录</span></div><div className="trace-timeline">{stages.map((stage,index)=><TimelineStage key={stage.id} stage={stage} index={index}/>)}</div>{!stages.length&&<p className="trace-empty">这项任务还没有执行记录。</p>}</main>:<main className={`trace-layout ${displayMode==='graph'&&embedded?'trace-graph-wide':''}`}><section className="trace-main"><div className="trace-view-tabs"><button className={mode==='path'?'selected':''} onClick={()=>setMode('path')}>本次调用路径</button><button className={mode==='architecture'?'selected':''} onClick={()=>setMode('architecture')}>项目架构</button></div><div className="trace-graph-card">{stages.length?mode==='path'?<PathGraph stages={stages} selected={chosen?.id||''} onSelect={(id)=>{setSelected(id);setDetailOpen(true);}}/>:<Architecture stages={stages} onFilter={setFilter}/>:<p className="trace-empty">还没有记录到执行阶段。</p>}</div>
      <div className="trace-list-heading"><h2>阶段记录</h2><select aria-label="按模块筛选轨迹" value={filter} onChange={e=>setFilter(e.target.value as StageKind|'all')}><option value="all">全部模块</option>{Object.entries(stageNames).map(([key,value])=><option key={key} value={key}>{value}</option>)}</select></div>
      <div className="trace-stage-list">{visible.map(s=><button key={s.id} className={`trace-stage-row ${chosen?.id===s.id?'chosen':''}`} onClick={()=>{setSelected(s.id);if(displayMode==='graph'&&embedded)setDetailOpen(true);}}><span className="trace-stage-number">{stages.indexOf(s)+1}</span><span><b>{s.title}</b><small>{s.subtitle||stageNames[s.kind]} · {s.events.length} 条事件</small></span><time>{time(s.events[0]?.created_at)}</time><em>{statusLabels[s.status]||s.status}</em></button>)}{!visible.length&&<p className="trace-empty">此模块没有对应事件记录。</p>}</div>
      <details className="trace-raw"><summary>查看原始事件 · {taskEvents.length} 条</summary>{taskEvents.map(e=><details key={e.event_id}><summary>#{e.seq} · {eventTitle(e)} · {time(e.created_at)}</summary><pre>{JSON.stringify(e,null,2)}</pre></details>)}</details>
    </section>{!(displayMode==='graph'&&embedded)&&<aside className="trace-detail"><Detail stage={chosen}/></aside>}</main>}
    {displayMode==='graph'&&embedded&&detailOpen&&<DetailDrawer stage={chosen} onClose={()=>setDetailOpen(false)}/>}
  </div>;
}

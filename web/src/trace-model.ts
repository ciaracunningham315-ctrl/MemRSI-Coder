export type TraceEvent = Record<string, any>;
export type StageKind = 'user' | 'planner' | 'coder' | 'tool' | 'memory' | 'skill' | 'context' | 'verification' | 'waiting' | 'end';
export type TraceStage = { id: string; seq: number; kind: StageKind; title: string; subtitle: string; status: string; events: TraceEvent[]; toolName?: string; inputLabel?: string };
export const stageNames: Record<StageKind, string> = { user:'用户消息',planner:'任务规划',coder:'代码 Agent',tool:'工具调用',memory:'历史检索',skill:'经验加载',context:'上下文整理',verification:'项目检查',waiting:'等待用户',end:'任务结果' };
const names: Record<string,string> = {read_file:'读取文件',edit_file:'修改文件',run_command:'执行命令',run_tests:'运行测试',memory_search:'搜索历史',replan:'请求重规划'};
function valueText(value:any):string { return typeof value==='string'?value:value==null?'':Array.isArray(value)?value.map(valueText).filter(Boolean).join('、'):''; }
export function decisionSummary(payload:Record<string,any>):string {
  const d=payload.decision;
  if(typeof d==='string')return d;
  const action=String(d?.action||'').toLowerCase();
  const actionLabel:Record<string,string>={respond:'直接回答',answer:'直接回答',ask_user:'向用户提问',question:'向用户提问',assign:'分配给编码器',delegate:'分配给编码器',end:'结束任务',done:'结束任务',coder:'交由编码器执行'};
  const parts:string[]=[];
  if(d?.answer)parts.push(`回答：${valueText(d.answer)}`);
  if(d?.question)parts.push(`问题：${valueText(d.question)}`);
  const assignment=d?.assignment;
  if(assignment){const target=valueText(assignment.role||assignment.node||assignment.assignee);const detail=valueText(assignment.summary||assignment.goal||assignment.task||assignment.prompt||assignment.step);parts.push(`分配${target?`给${target}`:'执行步骤'}${detail?`：${detail}`:''}`);}
  const plan=d?.plan;
  if(plan){const goal=valueText(plan.goal||plan.summary);const count=Array.isArray(plan.steps)?plan.steps.length:0;parts.push(`计划${goal?`：${goal}`:''}${count?`（${count} 个步骤）`:''}`);}
  const head=actionLabel[action]||action;
  if(head&&parts.length)parts.unshift(head);
  else if(head)parts.push(head);
  if(!parts.length&&payload.reason)parts.push(valueText(payload.reason));
  return parts.join(' · ')||'Planner 已选择下一步。';
}
export function reportSummary(payload:Record<string,any>):string {
  const r=payload.report;
  if(typeof r==='string')return r;
  if(!r||typeof r!=='object')return valueText(payload.summary||payload.content)||'编码器已提交本轮进度。';
  const parts:string[]=[];
  if(r.summary)parts.push(valueText(r.summary));
  if(r.step_id)parts.push(`步骤 ${valueText(r.step_id)}`);
  if(r.outcome)parts.push(`结果：${valueText(r.outcome)}`);
  if(Array.isArray(r.changed_files)&&r.changed_files.length)parts.push(`修改 ${r.changed_files.length} 个文件：${r.changed_files.map(valueText).join('、')}`);
  if(Array.isArray(r.checks)&&r.checks.length)parts.push(`检查：${r.checks.map((check:any)=>typeof check==='string'?check:valueText(check?.name||check?.command||check?.status)).filter(Boolean).join('、')}`);
  if(Array.isArray(r.artifact_refs)&&r.artifact_refs.length)parts.push(`${r.artifact_refs.length} 项产物引用`);
  return parts.join(' · ')||'编码器已提交本轮进度。';
}
function contextRole(payload:Record<string,any>):'planner'|'coder' {
  const role=String(payload.role||payload.node||'').toLowerCase();
  return role.includes('planner')?'planner':'coder';
}
function formatInput(tokens: unknown, estimated=false): string {
  if (typeof tokens !== 'number' || !Number.isFinite(tokens) || tokens < 0) return '输入 Token 未记录';
  return `${estimated?'估算输入':'输入'} ${(Math.round(tokens / 10) / 100).toFixed(2)}k Token`;
}
function reportedInput(usage?: Record<string,unknown>) {
  return usage?.prompt_tokens ?? usage?.input_tokens;
}
export function buildStages(source: TraceEvent[], taskId: string): TraceStage[] {
  const events=source.filter(e=>e.task_id===taskId).slice().sort((a,b)=>a.seq-b.seq);
  const stages: TraceStage[]=[]; const calls=new Map<string,TraceStage>(); const stepCalls=new Map<string,TraceStage>();
  const recordedUsage = new Map(events.filter(e=>e.event_type==='model.usage' && e.payload?.context_id)
    .map(e=>[e.payload.context_id,e.payload.input_tokens]));
  let legacyContext: {stage:TraceStage; role:'planner'|'coder'} | undefined;
  for(const e of events){
    const p=e.payload||{}; let kind:StageKind|null=null; let title=''; let subtitle=''; let status='recorded';
    if (legacyContext && ((e.event_type==='planner.decision' && legacyContext.role==='planner')
      || (e.event_type==='coder.message' && legacyContext.role==='coder'))) {
      const tokens = reportedInput(p.usage);
      if (typeof tokens==='number') legacyContext.stage.inputLabel = formatInput(tokens);
      legacyContext = undefined;
    }
    if(e.event_type==='planner.tool.started'){
      kind='planner';title='读取当前会话原文';subtitle=p.tool||'';status='unresolved';
    } else if(e.event_type==='planner.tool.completed'){
      const call=calls.get(p.tool_call_id);
      if(call){call.events.push(e);call.status=p.result?.status||'completed';call.subtitle=`读取 ${p.result?.messages?.length||0} 条会话消息`;continue;}
      kind='planner';title='会话原文读取结果';subtitle=p.result?.error||'';status=p.result?.status||'recorded';
      if(p.result?.status==='skipped'){title='跳过重复会话读取';subtitle=p.result.reason||'';}
      else if(p.result?.cached){title='复用已读取的会话原文';}
    } else if(e.event_type==='tool.started'){
      kind=p.tool==='memory_search'?'memory':String(p.tool_call_id).startsWith('acceptance-')?'verification':'tool';
      title=names[p.tool]||p.tool||'工具'; subtitle=p.tool||''; status='unresolved';
    } else if(['tool.completed','tool.failed','tool.resolved'].includes(e.event_type)){
      const call=calls.get(p.tool_call_id);
      if(call){call.events.push(e);call.status=e.event_type==='tool.failed'?'failed':p.resolution||p.status||'completed';continue;}
      kind='tool';title='工具结果';subtitle=p.tool_call_id||'';status=p.status||p.resolution||'recorded';
    } else if(e.event_type==='user.message'){kind='user';title=p.intent==='redirect'?'纠正方向':p.intent==='addendum'?'补充要求':'用户输入';subtitle=p.content||'';}
    else if(['planner.plan','planner.replanned','planner.decision'].includes(e.event_type)){kind='planner';title=e.event_type==='planner.replanned'?'Planner · 重规划':e.event_type==='planner.decision'?'Planner · 决策':'Planner · 制定计划';subtitle=e.event_type==='planner.decision'?decisionSummary(p):p.plan?.goal||p.summary||p.reason||'';}
    else if(['coder.message','coder.report'].includes(e.event_type)){kind='coder';title=e.event_type==='coder.report'?'Coder · 进度汇报':'Coder · 模型返回';subtitle=e.event_type==='coder.report'?reportSummary(p):p.message?.tool_calls?.length?`提出 ${p.message.tool_calls.length} 个工具调用`:p.content||p.message?.content||'空回复';}
    else if(e.event_type==='coder.report.only_requested'){kind='coder';title='停止重复巡检，整理报告';subtitle=(p.tools||[]).join('、');}
    else if(e.event_type==='assistant.question'){kind='waiting';title='等待用户回复';subtitle=valueText(p.content||p.question||p.summary)||'Agent 需要你补充信息。';status='waiting_user';}
    else if(e.event_type==='node.transition'){
      const from=String(p.from_node||p.from||'').toLowerCase(),to=String(p.to_node||p.to||'').toLowerCase();
      kind=to.includes('wait')?'waiting':to.includes('end')? 'end' : to.includes('coder')?'coder':to==='tool'?'tool':'planner';
      title=`${nodeLabel(from)} → ${nodeLabel(to)}`;subtitle=p.reason||p.summary||`切换到 ${nodeLabel(to)}`;status=to.includes('wait')?'waiting_user':to.includes('end')?'completed':'recorded';
    }
    else if(e.event_type==='plan.step.started'){kind='planner';title='执行计划步骤';subtitle=p.goal||p.step_id||'';status='running';}
    else if(e.event_type==='plan.step.completed'){
      const step=stepCalls.get(p.step_id);
      if(step){step.events.push(e);step.status='completed';continue;}
      kind='planner';title='完成计划步骤';subtitle=p.goal||p.result||p.step_id||'';status='completed';
    }
    else if(e.event_type==='code.patch'){
      const call=calls.get(p.tool_call_id);
      if(call){call.events.push(e);continue;}
      kind='tool';title='记录代码修改';subtitle=(p.file_paths||[]).join('、')||'代码差异';
    }
    else if(e.event_type==='context.assembled'){const role=contextRole(p);const snapshot=p.snapshot_id||p.context_id||p.artifact_id||p.snapshot_ref;kind='context';title=`组装 ${role === 'planner' ? 'Planner' : 'Coder'} 上下文`;subtitle=`${p.message_count??'—'} 条消息 · ${p.evidence_count??0} 条证据 · ${p.skill_count??0} 项技能`+(snapshot?` · 快照 ${valueText(snapshot)}`:'');}
    else if(e.event_type==='context.compacted'){kind='context';title='压缩上下文';subtitle=p.role||'';}
    else if(e.event_type==='model.usage'&&p.scope){kind='context';title=p.scope==='session'?'生成会话摘要':'生成执行摘要';subtitle=p.role||'';}
    else if(e.event_type==='memory.retrieved'){kind='memory';title='检索原始历史';subtitle=`${p.mode} · ${p.evidence_event_ids?.length || 0} 条证据`;status=p.status;}
    else if(e.event_type==='skill.loaded'){kind='skill';title='加载固定版本技能';subtitle=p.version_id||'';}
    else if(e.event_type==='verification.selected'){kind='verification';title='自动选择项目检查';subtitle=(p.checks||[]).map((check:any)=>`${check.name} · ${check.command?.join(' ')||''}`).join('；')||'没有找到可运行的检查';}
    else if(e.event_type==='verification.result'){kind='verification';title='项目检查结果';subtitle=p.coverage==='syntax_only'?'仅完成语法检查，未验证功能':p.reason||p.status;status=p.status;}
    else if(e.event_type==='verification.repair_requested'){kind='verification';title='检查失败 · 返回修复';subtitle=`第 ${p.attempt} 次修复`;status='failed';}
    else if(e.event_type==='task.error'){kind='end';title='执行出错';subtitle=p.message||'';status='failed';}
    else if(e.event_type==='task.updated'&&['completed','failed','cancelled','needs_review','interrupted','waiting_user'].includes(p.status)){
      const previous=stages[stages.length-1];if(previous?.kind==='end'&&previous.status===p.status)continue;
      kind=p.status==='waiting_user'?'waiting':'end';title=({completed:'任务完成',failed:'任务失败',cancelled:'任务已取消',needs_review:'等待副作用核对',interrupted:'任务中断',waiting_user:'等待用户回复'} as Record<string,string>)[p.status];status=p.status;
    }
    if(!kind)continue;
    const stage:TraceStage={id:e.event_id,seq:e.seq,kind,title,subtitle,status,events:[e]};stages.push(stage);
    if (e.event_type==='context.assembled') {
      legacyContext = undefined;
      if (recordedUsage.has(p.context_id)) {
        stage.inputLabel = formatInput(recordedUsage.get(p.context_id));
      } else {
        stage.inputLabel = typeof p.input_tokens_exact==='number' ? formatInput(p.input_tokens_exact)
          : formatInput(p.input_size_unit==='estimated_token_units'?p.input_size_estimate:undefined, true);
        legacyContext = {stage,role:contextRole(p)};
      }
    } else if (e.event_type==='context.compacted' && !p.input_tokens_recorded) {
      stage.inputLabel = formatInput(reportedInput(p.usage));
    } else if (e.event_type==='model.usage' && p.scope) {
      stage.inputLabel = formatInput(p.input_tokens);
    }
    if (['tool.started','planner.tool.started'].includes(e.event_type) && typeof p.tool==='string') stage.toolName=p.tool;
    if(e.event_type==='tool.started'||e.event_type==='planner.tool.started')calls.set(p.tool_call_id,stage);
    if(e.event_type==='plan.step.started')stepCalls.set(p.step_id,stage);
  }
  return stages;
}
function nodeLabel(value:string){
  const labels:Record<string,string>={planner:'Planner',coder:'Coder',coder_report:'Coder 汇报',tool:'Tool',end:'END',waiting_user:'等待用户',waiting:'等待用户'};
  return labels[value]||value||'节点';
}
export function eventTitle(e:TraceEvent){
  const titles:Record<string,string>={'user.message':'用户消息','user.message.consumed':'已收到补充要求','planner.plan':'制定计划','planner.replanned':'修订计划','planner.decision':'Planner 决策','coder.message':'模型回复','coder.report':'Coder 进度汇报','assistant.question':'Agent 提问','node.transition':'执行节点切换','tool.started':'开始调用工具','tool.completed':'工具执行完成','tool.failed':'工具执行失败','tool.resolved':'工具结果已核对','tool.interrupted':'工具执行中断','context.compacted':'整理较早的执行记录','context.assembled':'准备模型所需信息','memory.retrieved':'查找相关历史经验','skill.loaded':'加载相关经验','plan.step.started':'开始执行计划步骤','plan.step.completed':'完成计划步骤','code.patch':'记录代码修改','verification.result':'项目检查结果','verification.repair_requested':'根据检查结果继续修复','task.error':'执行异常','task.updated':'更新任务状态'};
  titles['context.assembled']='组装模型上下文';
  titles['planner.tool.started']='开始读取会话原文';
  titles['planner.tool.completed']='会话原文读取结果';
  titles['model.usage']='模型用量';
  titles['memory.retrieved']='检索原始历史';
  titles['verification.selected']='自动选择项目检查';
  titles['delivery.target_blocked']='需要确认工作区';
  titles['requirements.updated']='更新任务目标';
  titles['delivery.verification']='核对文件交付位置';
  titles['delivery.repair_requested']='补齐指定位置的文件';
  titles['coder.report.invalid']='执行报告格式需要纠正';
  titles['coder.report.retry_requested']='重新整理执行报告';
  titles['coder.report.only_requested']='交付和检查已有证据，跳过重复巡检';
  titles['coder.report.retry_tool_calls_rejected']='报告整理期间已阻止重复操作';
  return titles[e.event_type]||e.event_type;
}

/** Keep the original diagnostic in the trace; explain recoverable failures in chat. */
export function runtimeErrorMessage(error: unknown): string {
  const message = String(error || '执行遇到问题，请查看轨迹。');
  if (/Context budget exceeded|context_budget_exceeded/i.test(message)) {
    return '整理执行信息时超出了上下文预算，任务已中断。已保存的文件和执行记录仍在，可在修复后继续。';
  }
  if (/turn budget exhausted/i.test(message)) return '本次执行已达到轮数上限，进度已保存。请查看轨迹确认剩余工作。';
  if (/CoderReport|Coder response contains|Coder response has/i.test(message)) return '执行结果的格式未能通过校验，任务已中断。文件和操作记录已保存，可查看轨迹后继续。';
  return message;
}

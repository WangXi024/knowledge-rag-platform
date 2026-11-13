'use client';

/** 知识库图谱管理：保存受控配置并跟踪真实异步状态，不提供二期图谱浏览。 */
import { LoaderCircle, Network, RefreshCw } from 'lucide-react';
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import type { AdminSession, ApiEnvelope, GraphConfiguration, GraphStatus, KnowledgeBase, KnowledgeGraphState } from '@/lib/contracts';
import { errorMessage, gatewayRequest } from '@/lib/gateway-api';

const IN_FLIGHT = new Set<GraphStatus>(['QUEUED', 'BUILDING', 'CANCELLING']);
const STATUS_LABELS: Record<GraphStatus, string> = {
  DISABLED: '未启用', NOT_BUILT: '尚未构建', QUEUED: '等待构建', BUILDING: '构建中',
  READY: '可用', STALE: '内容已变更，待重建', FAILED: '构建失败', CANCELLING: '取消待确认',
};

/** 只格式化真实服务端时间；无效时间保留缺省值，不使用浏览器当前时间替代。 */
function displayTime(value: string | null): string {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString('zh-CN');
}

/** 父组件按身份和知识库重挂载；请求序号额外阻止迟到读响应覆盖新的写操作。 */
export function KnowledgeGraphSettings({ session, knowledgeBase, available }: {
  session: AdminSession;
  knowledgeBase: KnowledgeBase;
  available: boolean;
}) {
  const [state, setState] = useState<KnowledgeGraphState | null>(null);
  const [draft, setDraft] = useState<GraphConfiguration | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState('load');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [requestId, setRequestId] = useState('');
  const mounted = useRef(true);
  const sequence = useRef(0);
  const dirtyRef = useRef(false);
  const path = `v1/management/knowledge-bases/${knowledgeBase.id}/graph`;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; sequence.current += 1; };
  }, []);

  const refresh = useCallback(async (quiet = false) => {
    const current = ++sequence.current;
    if (!quiet) setBusy('load');
    try {
      const response = await gatewayRequest<ApiEnvelope<KnowledgeGraphState>>(session, path);
      if (!mounted.current || current !== sequence.current) return;
      setState(response.data);
      setRequestId(response.request_id);
      // 后台轮询不得覆盖管理员尚未保存的开关。
      if (!dirtyRef.current) setDraft(response.data.configuration);
      setError('');
    } catch (failure) {
      if (mounted.current && current === sequence.current) setError(errorMessage(failure));
    } finally {
      if (mounted.current && current === sequence.current && !quiet) setBusy('');
    }
  }, [path, session]);

  useEffect(() => {
    const timeout = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timeout);
  }, [refresh]);

  useEffect(() => {
    if (busy || !state || (!state.configuration.enabled && !IN_FLIGHT.has(state.status))) return;
    // 在途任务持续轮询；已启用的空闲图谱也检查内容失效，避免长期显示旧 READY。
    const timeout = window.setTimeout(() => void refresh(true), IN_FLIGHT.has(state.status) ? 2500 : 10000);
    return () => window.clearTimeout(timeout);
  }, [busy, state, refresh]);

  /** 单次提交只显示服务器返回的阶段，202 绝不直接标记为构建完成。 */
  async function submit(action: 'configuration' | 'build' | 'cancel') {
    if (busy || !draft) return;
    const current = ++sequence.current;
    setBusy(action);
    setError('');
    setNotice('');
    try {
      const response = await gatewayRequest<ApiEnvelope<KnowledgeGraphState>>(session, `${path}/${action}`, {
        method: action === 'configuration' ? 'PUT' : 'POST',
        ...(action === 'configuration' ? { body: { ...draft } } : {}),
      });
      if (!mounted.current || current !== sequence.current) return;
      setState(response.data);
      setRequestId(response.request_id);
      if (action === 'configuration') {
        dirtyRef.current = false;
        setDirty(false);
        setDraft(response.data.configuration);
      }
      setNotice(action === 'configuration' ? '配置已保存，请查看下方实际构建状态。'
        : action === 'cancel' ? '取消请求已提交，等待引擎确认结束。' : '构建请求已提交，请等待任务完成。');
    } catch (failure) {
      if (mounted.current && current === sequence.current) setError(errorMessage(failure));
    } finally {
      if (mounted.current && current === sequence.current) setBusy('');
    }
  }

  /** 草稿固定使用已批准的 light@1 策略，浏览器不接收任意上游 Parser 参数。 */
  function change(field: 'enabled' | 'auto_build' | 'retrieval_enabled', value: boolean) {
    if (!draft) return;
    dirtyRef.current = true;
    setDirty(true);
    setDraft({ ...draft, [field]: value });
    setNotice('');
  }

  const inFlight = Boolean(state && IN_FLIGHT.has(state.status));
  // 父列表可能停留在上传被接收时的旧版本。图谱刷新以后台状态及同次响应的版本为准，
  // 不拿旧列表误判过期；若列表已知更新版本，则仍保守等待新图状态，后台 STALE 也保持原样。
  const isCurrent = state?.built_for_revision != null && state.built_for_revision === state.target_content_revision && knowledgeBase.revision <= state.target_content_revision;
  const status = state?.status === 'READY' && !isCurrent ? 'STALE' : state?.status;
  // 内容版本只会递增，展示列表与图谱接口已观察到的较新版本，避免显示“已构建 2/当前 1”。
  const contentRevision = Math.max(knowledgeBase.revision, state?.target_content_revision ?? 0);
  const canBuild = available && state?.configuration.enabled && !busy && !inFlight && !dirty && !error;
  const buildLabel = state?.status === 'FAILED' ? '重试构建' : state?.built_for_revision == null ? '构建图谱' : '重建图谱';

  return (
    <section className="graph-settings" aria-label={`${knowledgeBase.name}的知识图谱`}>
      <header>
        <div><span className="graph-eyebrow"><Network size={16} />原生 GraphRAG</span><h2>{knowledgeBase.name} · 知识图谱</h2></div>
        <button className="secondary-button" onClick={() => void refresh()} disabled={Boolean(busy)}><RefreshCw size={16} />刷新图谱状态</button>
      </header>
      <p className="graph-description">从文档提取实体与关系，辅助跨文档检索。构建会调用配置的模型；文档内容变更后需要重建。</p>
      {!available && <p className="graph-note">服务尚未启用原生图谱能力或未完成 Chat 模型登记，请先完成服务配置。已存在的任务仍可查看和取消。</p>}
      {error && <div className="graph-error" role="alert">{error}</div>}
      {notice && <p className="graph-note" role="status">{notice}</p>}
      {!state || !draft ? <p role="status">{busy ? '正在读取图谱状态…' : '图谱状态暂不可用，请刷新重试。'}</p> : (
        <>
          <div className="graph-summary" aria-live="polite">
            <strong className={`graph-status graph-status-${status?.toLowerCase()}`}>{status ? STATUS_LABELS[status] : '未知状态'}</strong>
            <span>当前内容版本 {contentRevision}</span>
            <span>构图目标版本 {state.target_content_revision}</span>
            <span>已构建内容版本 {state.built_for_revision ?? '—'}</span>
            <span>图谱代数 {state.graph_generation}</span>
          </div>
          {status === 'CANCELLING' && <p className="graph-note">取消尚未确认。关闭前端页面不会终止引擎任务，取消请求也不代表模型调用已经停止。</p>}
          {status === 'STALE' && <p className="graph-note">现有图谱与当前内容版本不一致，完成重建前不会作为当前图谱参与检索。</p>}
          {state.last_error && <p className="graph-error" role="alert">{state.last_error.message}（{state.last_error.code}）</p>}
          <form onSubmit={(event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void submit('configuration'); }}>
            <fieldset disabled={Boolean(busy)}>
              <legend className="sr-only">知识库图谱配置</legend>
              <label><input type="checkbox" checked={draft.enabled} disabled={!available && !draft.enabled} onChange={(event) => change('enabled', event.target.checked)} /><span><strong>启用知识图谱</strong><small>使用标准轻量构图策略</small></span></label>
              <label><input type="checkbox" checked={draft.auto_build} disabled={!draft.enabled} onChange={(event) => change('auto_build', event.target.checked)} /><span><strong>自动构建</strong><small>保存配置与文档变更后自动安排构建</small></span></label>
              <label><input type="checkbox" checked={draft.retrieval_enabled} disabled={!draft.enabled} onChange={(event) => change('retrieval_enabled', event.target.checked)} /><span><strong>用于问答检索</strong><small>仅当前内容版本的可用图谱参与检索</small></span></label>
            </fieldset>
            <div className="graph-actions">
              <button type="submit" className="primary-button" disabled={Boolean(busy) || !dirty || (!available && draft.enabled)}>{busy === 'configuration' && <LoaderCircle size={16} className="spin" />}保存图谱配置</button>
              <button type="button" className="secondary-button" disabled={!canBuild} onClick={() => void submit('build')}>{busy === 'build' && <LoaderCircle size={16} className="spin" />}{buildLabel}</button>
              <button type="button" className="secondary-button" disabled={Boolean(busy) || !inFlight || state.status === 'CANCELLING'} onClick={() => void submit('cancel')}>取消构建</button>
              {dirty && <span>配置尚未保存</span>}
            </div>
          </form>
          <details className="graph-diagnostics"><summary>诊断信息</summary><dl>
            <div><dt>构图策略</dt><dd>{state.configuration.profile_ref}</dd></div>
            <div><dt>配置版本</dt><dd>{state.configuration_version}</dd></div>
            <div><dt>任务 ID</dt><dd>{state.job_id ?? '—'}</dd></div>
            <div><dt>请求 ID</dt><dd>{requestId || '—'}</dd></div>
            <div><dt>入队时间</dt><dd>{displayTime(state.queued_at)}</dd></div>
            <div><dt>开始时间</dt><dd>{displayTime(state.started_at)}</dd></div>
            <div><dt>完成时间</dt><dd>{displayTime(state.completed_at)}</dd></div>
            <div><dt>状态更新时间</dt><dd>{displayTime(state.updated_at)}</dd></div>
          </dl></details>
        </>
      )}
    </section>
  );
}

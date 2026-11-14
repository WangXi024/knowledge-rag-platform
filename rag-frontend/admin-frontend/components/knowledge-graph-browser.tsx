'use client';

/** 只读图谱与本平台社区摘要；视图固定图代数，切换范围/版本立即清除旧内容。 */
import { FormEvent, useEffect, useRef, useState } from 'react';
import type { AdminSession, AggregateEvidence, ApiEnvelope, GraphCommunities, GraphEdge, GraphNode, GraphSource, GraphView, KnowledgeBase, KnowledgeGraphState } from '@/lib/contracts';
import { errorMessage, gatewayRequest } from '@/lib/gateway-api';
import { GraphSourceReader } from '@/components/graph-source-reader';
import { sourcePages } from '@/lib/pdf-positions';

const STATUS: Record<string, string> = { DISABLED: '尚未启用', NOT_BUILT: '尚未构建', QUEUED: '等待构建', BUILDING: '正在构建', STALE: '内容已变更，请先重建', FAILED: '构建失败，请检查任务', CANCELLING: '取消待确认' };

/** 将不透明实体 ID 交给后端；不把实体名或描述拼接为接口路径。 */
export function graphViewPath(id: string, query: string, depth: number, generation: number, nodeId = ''): string {
  const params = new URLSearchParams({ query: query.slice(0, 100), depth: String(depth), limit: '40', generation: String(generation) });
  if (nodeId) params.set('node_id', nodeId);
  return `v1/management/knowledge-bases/${encodeURIComponent(id)}/graph/view?${params}`;
}

function Sources({ sources, onOpen }: { sources: GraphSource[]; onOpen: (source: GraphSource) => void }) {
  return sources.length ? <ul className="graph-source-list">{sources.map((source) => <li key={`${source.document_id}:${source.chunk_id}`}>
    <button className="secondary-button" onClick={() => onOpen(source)}>{source.document_name} · {sourcePages(source.positions).length ? `第 ${sourcePages(source.positions).join('、')} 页` : '无页码'} · 查看原文</button>
  </li>)}</ul> : <p className="graph-note">没有可用原文来源，不能作为回答证据。</p>;
}

export function KnowledgeGraphBrowser({ session, knowledgeBase, state }: {
  session: AdminSession; knowledgeBase: KnowledgeBase; state: KnowledgeGraphState | null;
}) {
  const [view, setView] = useState<GraphView | null>(null);
  const [tab, setTab] = useState<'graph' | 'communities'>('graph');
  const [query, setQuery] = useState('');
  const [depth, setDepth] = useState(1);
  const [selected, setSelected] = useState<GraphNode | GraphEdge | null>(null);
  const [communities, setCommunities] = useState<GraphCommunities | null>(null);
  const [community, setCommunity] = useState<AggregateEvidence | null>(null);
  const [source, setSource] = useState<GraphSource | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const path = `v1/management/knowledge-bases/${encodeURIComponent(knowledgeBase.id)}/graph`;
  const ready = state?.status === 'READY' && state.configuration.enabled !== false;
  const generation = state?.graph_generation ?? 0;

  // 只读面板不轮询模型；一期状态卡负责轮询，状态或图代数改变后旧视图立即失效。
  useEffect(() => {
    sequence.current++; pending.current?.abort();
    const timer = window.setTimeout(() => {
      setView(null); setSelected(null); setCommunities(null); setCommunity(null); setSource(null); setError(''); setBusy(false);
    }, 0);
    return () => { window.clearTimeout(timer); pending.current?.abort(); };
  }, [knowledgeBase.id, generation, state?.status, state?.configuration_version, session]);

  /** 每次只发送一个只读请求，取消前一请求；迟到响应不得覆盖新图或新的搜索。 */
  async function read<T>(url: string, apply: (value: T) => void) {
    pending.current?.abort();
    const controller = new AbortController(); pending.current = controller;
    const current = ++sequence.current;
    setBusy(true); setError(''); setSource(null);
    try {
      const response = await gatewayRequest<ApiEnvelope<T>>(session, url, { signal: controller.signal });
      if (!controller.signal.aborted && current === sequence.current) apply(response.data);
    } catch (failure) {
      if (!controller.signal.aborted && current === sequence.current) {
        setView(null); setSelected(null); setCommunities(null); setCommunity(null); setError(errorMessage(failure));
      }
    } finally { if (!controller.signal.aborted && current === sequence.current) setBusy(false); }
  }

  async function browse(nodeId = '') {
    if (!ready) return;
    setSelected(null); setView(null);
    await read<GraphView>(graphViewPath(knowledgeBase.id, query, depth, generation, nodeId), (value) => {
      if (value.knowledge_base_id !== knowledgeBase.id || value.graph_generation !== generation
          || value.nodes.length > 40 || value.edges.length > 127) throw new Error('图谱范围或版本不一致，请刷新状态后重试。');
      setView(value);
    });
  }

  async function loadCommunities() {
    if (!ready) return;
    setCommunity(null); setCommunities(null);
    await read<GraphCommunities>(`${path}/communities?generation=${generation}`, (value) => {
      if (value.knowledge_base_id !== knowledgeBase.id || value.graph_generation !== generation) throw new Error('社区版本已变化，请刷新状态。');
      setCommunities(value);
    });
  }

  async function loadCommunity(id: string) {
    setCommunity(null);
    await read<AggregateEvidence>(`${path}/communities/${encodeURIComponent(id)}?generation=${generation}`, (value) => {
      if (value.knowledge_base_id !== knowledgeBase.id || value.graph_generation !== generation
          || value.kind !== 'community_aggregate' || value.origin !== 'gateway_generated') throw new Error('社区来源或版本无法核验。');
      setCommunity(value);
    });
  }

  const currentView = ready && view?.status === 'READY' && view.graph_generation === generation
    && view.content_revision === state?.built_for_revision ? view : null;
  const currentCommunities = ready && communities?.graph_generation === generation
    && communities.content_revision === state?.built_for_revision ? communities : null;
  const currentCommunity = ready && community?.graph_generation === generation
    && community.content_revision === state?.built_for_revision ? community : null;
  const nodes = currentView?.nodes ?? [];
  const edges = currentView?.edges ?? [];
  const coordinates = new Map(nodes.map((node, index) => [node.id, {
    x: 300 + 235 * Math.cos(index * 2 * Math.PI / Math.max(nodes.length, 1)),
    y: 180 + 125 * Math.sin(index * 2 * Math.PI / Math.max(nodes.length, 1)),
  }]));

  return <section className="graph-browser" aria-label={`${knowledgeBase.name}只读图谱浏览`}>
    <header><div><h2>图谱与社区</h2><p>实体与关系描述是模型生成内容；有效证据以当前原文为准。</p></div>
      <div className="graph-tabs"><button aria-pressed={tab === 'graph'} onClick={() => setTab('graph')}>实体与关系</button><button aria-pressed={tab === 'communities'} onClick={() => setTab('communities')}>社区摘要</button></div></header>
    <p className="graph-note">图谱代数 {generation} · 内容版本 {state?.built_for_revision ?? '—'} · {state?.configuration.profile_ref ?? '—'}</p>
    {!state ? <p role="status">正在读取图谱状态…</p> : !ready ? <p role="status">{STATUS[state.status] ?? '图谱暂不可用'}。当前不展示旧图内容。</p> : <>
      {tab === 'graph' ? <>
        <form className="graph-search" onSubmit={(event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void browse(); }}>
          <label>实体搜索<input maxLength={100} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入实体名，或留空浏览" /></label>
          <label>邻域深度<select value={depth} onChange={(event) => setDepth(Number(event.target.value))}><option value={0}>仅实体</option><option value={1}>一跳邻居</option><option value={2}>两跳邻居</option></select></label>
          <button className="secondary-button" disabled={busy}>搜索 / 刷新图谱</button>
        </form>
        {view && view.status !== 'READY' && <p role="status">图谱当前不可用，请刷新状态后重试。</p>}
        {view?.status === 'READY' && !currentView && <p role="status">图谱内容版本已变化，请刷新图谱状态。</p>}
        {currentView && <>{currentView.truncated && <p className="graph-note">本次仅展示有界邻域，已达到显示上限；可缩小搜索或展开深度。</p>}
          <div className="graph-workbench"><div>
            {nodes.length ? <svg className="graph-canvas" viewBox="0 0 600 360" role="img" aria-label="只读实体关系图；下方列表提供相同操作">
              {edges.map((edge) => { const from = coordinates.get(edge.source), to = coordinates.get(edge.target); return from && to ? <line key={edge.id} x1={from.x} y1={from.y} x2={to.x} y2={to.y} /> : null; })}
              {nodes.map((node) => { const point = coordinates.get(node.id)!; return <g key={node.id} role="button" tabIndex={0} aria-label={`选择实体 ${node.name}`} onClick={() => setSelected(node)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelected(node); } }}>
                <circle cx={point.x} cy={point.y} r={selected?.id === node.id ? 13 : 9} /><text x={point.x} y={point.y - 17} textAnchor="middle">{node.name.length > 12 ? `${node.name.slice(0, 12)}…` : node.name}</text><title>{node.name}</title>
              </g>; })}</svg> : <p role="status">没有匹配实体或可用图内容。</p>}
            <ul className="graph-entity-list">{nodes.map((node) => <li key={node.id}><button aria-pressed={selected?.id === node.id} onClick={() => setSelected(node)}>{node.name} <small>{node.entity_type}</small></button></li>)}</ul>
            <details><summary>关系列表（{edges.length}）</summary><ul className="graph-entity-list">{edges.map((edge) => <li key={edge.id}><button onClick={() => setSelected(edge)}>{nodes.find((node) => node.id === edge.source)?.name ?? '实体'} ↔ {nodes.find((node) => node.id === edge.target)?.name ?? '实体'}</button></li>)}</ul></details>
          </div><aside className="graph-detail" aria-live="polite">{selected ? <><h3>{'name' in selected ? selected.name : '关系详情'}</h3><span className="generated-badge">生成描述</span><p className="graph-description-text">{selected.description || '暂无描述'}</p>
            {'name' in selected && <button className="secondary-button" disabled={busy} onClick={() => void browse(selected.id)}>展开此实体邻域</button>}
            <h4>当前可用原文</h4>{selected.source_status === 'available' ? <Sources sources={selected.sources} onOpen={setSource} /> : <p className="graph-note">来源已失效或不可用；不显示旧原文。</p>}
          </> : <p>选择一个实体或关系查看详情与来源。</p>}</aside></div>
          {currentView.warnings.length > 0 && <details><summary>图谱提示</summary><p>{currentView.warnings.join(' · ')}</p></details>}
        </>}
      </> : <>
        <p>本平台生成社区摘要：根据原生图连通社区和当前原文生成，不是 RAGFlow 原生报告。</p>
        <button className="secondary-button" disabled={busy} onClick={() => void loadCommunities()}>读取 / 刷新社区摘要</button>
        {communities && !currentCommunities && <p role="status">社区内容或图谱状态已变化，请刷新图谱状态。</p>}
        {currentCommunities && (currentCommunities.status !== 'READY' ? <p role="status">社区当前不可用，请刷新图谱状态。</p> : <>
          {!currentCommunities.communities.length && <p role="status">暂无可用社区摘要；需使用含社区摘要的构图策略完成构建。</p>}
          <ul className="graph-community-list">{currentCommunities.communities.map((item) => <li key={item.community_id}><button onClick={() => void loadCommunity(item.community_id)} disabled={busy}>{item.title} · {item.sources.length} 个原文来源</button></li>)}</ul>
          {currentCommunities.warnings.length > 0 && <p className="graph-note">{currentCommunities.warnings.join(' · ')}</p>}
        </>)}
        {currentCommunity && <article className="community-detail"><span className="generated-badge">本平台生成社区摘要</span><h3>{currentCommunity.title}</h3><p className="graph-description-text">{currentCommunity.summary}</p>
          <dl><dt>版本</dt><dd>图 {currentCommunity.graph_generation} / 内容 {currentCommunity.content_revision} / 配置 {currentCommunity.configuration_version}</dd><dt>构图策略</dt><dd>{currentCommunity.graph_profile_ref}</dd><dt>生成模型与策略</dt><dd>{currentCommunity.generator_model_ref} · {currentCommunity.strategy_ref}</dd></dl>
          <h4>原文来源</h4><Sources sources={currentCommunity.sources} onOpen={setSource} /></article>}
      </>}
      {busy && <p role="status">正在读取并核验图谱来源…</p>}
    </>}
    {error && <p className="graph-error" role="alert">{error} 请刷新图谱状态后重试。</p>}
    {source && (currentView || currentCommunity) && <GraphSourceReader key={`${source.document_id}:${source.document_revision}:${source.chunk_id}`} session={session} source={source} onClose={() => setSource(null)} />}
  </section>;
}

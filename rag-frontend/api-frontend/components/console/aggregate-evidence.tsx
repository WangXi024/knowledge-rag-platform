'use client';

/** 诊断控制台独立展示派生摘要；来源按修订回读，不将 summary 放入普通引用 quote。 */
import { useEffect, useRef, useState } from 'react';
import { gatewayRequest } from '@/lib/client-api';

type Source = {
  document_id: string; document_name: string; document_revision: number; chunk_id: string; quote: string;
};
export type AggregateEvidence = {
  aggregate_id: string; label: string; kind: 'community_aggregate'; derived: true;
  origin: 'gateway_generated'; title: string; summary: string;
  graph_generation: number; content_revision: number; configuration_version: number;
  graph_profile_ref: string; generator_model_ref: string; strategy_ref: string; sources: Source[];
};

export function aggregateEvents(current: AggregateEvidence[], incoming: unknown): AggregateEvidence[] {
  const values = Array.isArray(incoming) ? incoming : [incoming];
  const map = new Map(current.map((item) => [item.aggregate_id, item]));
  for (const value of values) {
    if (!value || typeof value !== 'object') throw new Error('聚合证据格式无效');
    const item = value as AggregateEvidence;
    if (item.origin !== 'gateway_generated' || item.kind !== 'community_aggregate' || item.derived !== true
        || !/^A[1-9][0-9]*$/u.test(item.label) || !Array.isArray(item.sources) || item.sources.length < 2 || item.sources.length > 16) throw new Error('聚合证据来源无效');
    map.set(item.aggregate_id, item);
  }
  return [...map.values()];
}

export function AggregateEvidencePanel({ items }: { items: AggregateEvidence[] }) {
  const [reading, setReading] = useState('');
  const [verified, setVerified] = useState<{ key: string; quote: string } | null>(null);
  const [error, setError] = useState('');
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  async function readSource(source: Source) {
    if (reading) return;
    const key = `${source.document_id}:${source.chunk_id}`;
    const controller = new AbortController(); pending.current = controller;
    setReading(key); setError(''); setVerified(null);
    try {
      for (let page = 1; page <= 5; page++) {
        const response = await gatewayRequest<{ data: Array<{ id: string; content: string }>; meta: { total: number; page_size: number } }>(
          `v1/documents/${encodeURIComponent(source.document_id)}/chunks?page=${page}&page_size=20&document_revision=${source.document_revision}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (response.data.some((chunk) => chunk.id === source.chunk_id && Boolean(source.quote.trim()) && chunk.content.includes(source.quote))) { setVerified({ key, quote: source.quote }); return; }
        if (page * response.meta.page_size >= response.meta.total) break;
      }
      throw new Error('当前回读范围内没有有效原文');
    } catch { if (!controller.signal.aborted) setError('来源已变化、不可访问或暂时无法核验。'); }
    finally { if (!controller.signal.aborted) setReading(''); }
  }
  return <div>{items.map((item) => <article className="citation-card aggregate-evidence" key={item.aggregate_id}>
    <strong>{item.label} · 本平台生成社区摘要</strong><h3>{item.title}</h3><p style={{ whiteSpace: 'pre-wrap' }}>{item.summary}</p>
    <div className="hint">派生内容，非 RAGFlow 原生报告 · 图 {item.graph_generation} / 内容 {item.content_revision} / 配置 {item.configuration_version}</div>
    <div className="hint">{item.graph_profile_ref} · {item.generator_model_ref} · {item.strategy_ref}</div>
    {item.sources.map((source) => { const key = `${source.document_id}:${source.chunk_id}`; return <div key={key} style={{ marginTop: 10 }}>
      <button className="button secondary small" disabled={Boolean(reading)} onClick={() => void readSource(source)}>{source.document_name} · 回读原文（修订 {source.document_revision}）</button>
      {verified?.key === key && <blockquote style={{ whiteSpace: 'pre-wrap' }}>{verified.quote}</blockquote>}
    </div>; })}
  </article>)}{reading && <p role="status">正在核验原文来源…</p>}{error && <p role="alert">{error}</p>}</div>;
}

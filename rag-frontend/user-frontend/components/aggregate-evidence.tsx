'use client';

/** 生成摘要与原文分区呈现；每个原文来源打开时按当前修订重新回读，失败不展示旧摘录。 */
import { useEffect, useRef, useState } from 'react';
import type { AggregateEvidence, AggregateSource, UserDocumentChunk } from '@/lib/ui-types';

export function AggregateEvidenceCards({ items, activeLabel }: { items: AggregateEvidence[]; activeLabel?: string }) {
  const [source, setSource] = useState<AggregateSource | null>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => setSource(null), 0);
    return () => window.clearTimeout(timer);
  }, [items]);
  return <>
    {items.map((item) => <article className={`aggregate-card${activeLabel === item.label ? ' active' : ''}`} id={`evidence-${item.label}`} key={item.aggregate_id}>
      <span className="aggregate-badge">{item.label} · 本平台生成社区摘要</span><h3>{item.title}</h3>
      <p>{item.summary}</p><small>派生内容，非原文摘录 · 图 {item.graph_generation} / 内容 {item.content_revision}</small>
      <details><summary>生成信息与原文来源（{item.sources.length}）</summary>
        <p className="aggregate-meta">{item.graph_profile_ref} · {item.generator_model_ref} · {item.strategy_ref}</p>
        <p className="aggregate-meta">原文可用性以打开时重新核验为准。</p>
        <ul>{item.sources.map((entry) => <li key={`${entry.document_id}:${entry.chunk_id}`}><button type="button" onClick={() => setSource(entry)}>{entry.document_name} · 查看原文</button></li>)}</ul>
      </details>
    </article>)}
    {source && <AggregateSourceReader key={`${source.document_id}:${source.document_revision}:${source.chunk_id}`} source={source} onClose={() => setSource(null)} />}
  </>;
}

function AggregateSourceReader({ source, onClose }: { source: AggregateSource; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [verified, setVerified] = useState<UserDocumentChunk | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const previous = document.activeElement;
    const overflow = document.body.style.overflow;
    const current = dialog.current;
    current?.showModal(); document.body.style.overflow = 'hidden';
    return () => {
      current?.close(); document.body.style.overflow = overflow;
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      for (let page = 1; page <= 5; page++) {
        const params = new URLSearchParams({ page: String(page), page_size: '20',
          document_revision: String(source.document_revision) });
        if (source.active_version_id !== null) params.set('active_version_id', source.active_version_id);
        const response = await fetch(`/api/documents/${encodeURIComponent(source.document_id)}/chunks?${params}`, { signal: controller.signal, cache: 'no-store' });
        const body = await response.json() as { data?: UserDocumentChunk[]; meta?: { total: number; pageSize: number }; error?: string };
        if (!response.ok) throw new Error(body.error || '来源暂不可用');
        const found = body.data?.find((chunk) => chunk.id === source.chunk_id && chunk.content.includes(source.quote));
        if (controller.signal.aborted) return;
        if (found) { setVerified(found); return; }
        if (!body.meta || page * body.meta.pageSize >= body.meta.total) break;
      }
      throw new Error('未在本次回读范围找到原文，来源可能已变化，请重新提问或查看文档。');
    })().catch((failure: unknown) => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : '来源暂不可用'); });
    return () => controller.abort();
  }, [source]);
  return <dialog ref={dialog} className="aggregate-source-dialog" aria-label="社区摘要的原文来源"
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <header><h2>{source.document_name}</h2><button onClick={onClose} aria-label="关闭原文来源">×</button></header>
    {error ? <p role="alert">{error}</p> : verified ? <><p>已核验当前文档修订 {source.document_revision} · {verified.pages.length ? `第 ${verified.pages.join('、')} 页` : '无页码信息'}</p><blockquote>{source.quote}</blockquote></> : <p role="status">正在回读当前活动版本的原文…</p>}
  </dialog>;
}

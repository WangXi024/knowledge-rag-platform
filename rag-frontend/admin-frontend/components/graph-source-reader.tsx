'use client';

/** 图谱来源只读回读：绑定修订，串行查找有限原文页；未核验的摘录不展示为有效原文。 */
import { useEffect, useRef, useState } from 'react';
import type { AdminSession, DocumentChunk, GraphSource, PageEnvelope } from '@/lib/contracts';
import { errorMessage, gatewayRequest } from '@/lib/gateway-api';
import { sourcePages } from '@/lib/pdf-positions';
import { PdfSourcePreview } from '@/components/pdf-source-preview';

export function GraphSourceReader({ session, source, onClose }: {
  session: AdminSession; source: GraphSource; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [chunk, setChunk] = useState<DocumentChunk | null>(null);
  const [error, setError] = useState('');
  const [page, setPage] = useState(sourcePages(source.positions)[0] ?? 1);
  useEffect(() => {
    const previous = document.activeElement;
    const overflow = document.body.style.overflow;
    const current = dialog.current;
    current?.showModal();
    document.body.style.overflow = 'hidden';
    return () => {
      current?.close(); document.body.style.overflow = overflow;
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      // 一次最多回读五页，避免为一个来源下载整篇超大文档。超出预算明确提示。
      for (let page = 1; page <= 5; page++) {
        const response = await gatewayRequest<PageEnvelope<DocumentChunk>>(session,
          `v1/management/documents/${encodeURIComponent(source.document_id)}/chunks?page=${page}&page_size=20&document_revision=${source.document_revision}`,
          { signal: controller.signal });
        if (controller.signal.aborted) return;
        const found = response.data.find((item) => item.document_id === source.document_id
          && item.id === source.chunk_id && Boolean(source.quote.trim()) && item.content.includes(source.quote));
        if (found) { setChunk(found); return; }
        if (page * response.meta.page_size >= response.meta.total) break;
      }
      throw new Error('未在本次回读范围找到对应原文，请刷新图谱或从文档列表查看切块。');
    })().catch((failure: unknown) => { if (!controller.signal.aborted) setError(errorMessage(failure)); });
    return () => controller.abort();
  }, [session, source]);
  const pdf = source.document_name.toLowerCase().endsWith('.pdf');
  return <dialog ref={dialog} className={`chunk-drawer${pdf ? ' with-source' : ''}`} aria-label="图谱原文来源"
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <header><div><span>原文来源 · 文档修订 {source.document_revision}</span><h2>{source.document_name}</h2></div>
      <button aria-label="关闭原文来源" onClick={onClose}>×</button></header>
    <div className="chunk-workbench">
      {pdf && chunk && <PdfSourcePreview session={session} documentId={source.document_id}
        revision={source.document_revision} selectedChunk={chunk} page={page} onPageChange={setPage} />}
      <section className="chunk-list" aria-live="polite">
        {error ? <p className="graph-error" role="alert">来源暂不可用：{error}</p>
          : chunk ? <article className="chunk-card"><header><strong>已逐字回读原文</strong><span>{sourcePages(chunk.positions).length ? `第 ${sourcePages(chunk.positions).join('、')} 页` : '无页码信息'}</span></header><div className="chunk-content">{source.quote}</div></article>
            : <p role="status">正在核验当前活动版本的原文…</p>}
      </section>
    </div>
  </dialog>;
}

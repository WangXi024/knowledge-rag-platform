'use client';

/**
 * 文档切块只读抽屉：沿用用户端的顶部信息、滚动卡片和固定分页布局。
 * 管理端仍用当前身份经同源代理分页查询；原生 dialog 负责模态焦点与背景隔离。
 * 不上传、修改或重新解析文档；卸载/翻页后忽略旧响应，正文不会作为 HTML 执行。
 */
import { AlertCircle, ChevronLeft, ChevronRight, Layers3, RefreshCw, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { AdminSession, DocumentChunk, PageEnvelope } from '@/lib/contracts';
import { errorMessage, gatewayRequest } from '@/lib/gateway-api';
import { sourcePages } from '@/lib/pdf-positions';
import { PdfSourcePreview } from '@/components/pdf-source-preview';

type ChunkState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; result: PageEnvelope<DocumentChunk> };

/**
 * session/documentId 决定读取范围；filename/chunkCount 来自文档列表，先展示概要。
 * PDF 附 documentRevision 后开启原文双栏，并让原文件与切块请求使用同一修订。
 * onClose 通知父组件清空选择；每次挂载从第一页读取，接口返回后以真实总数为准。
 * 失败可重试；关闭会恢复背景滚动和触发按钮焦点，并忽略尚未返回的结果。
 */
export function DocumentChunks({ session, documentId, filename, chunkCount, documentRevision, onClose }: {
  session: AdminSession; documentId: string; filename: string; chunkCount: number; documentRevision?: number; onClose: () => void;
}) {
  const [query, setQuery] = useState({ page: 1 });
  const [state, setState] = useState<ChunkState>({ status: 'loading' });
  const [meta, setMeta] = useState({ page: 1, page_size: 20, total: chunkCount });
  const [selectedChunk, setSelectedChunk] = useState<DocumentChunk | null>(null);
  const [sourcePage, setSourcePage] = useState(1);
  const hasSource = filename.toLowerCase().endsWith('.pdf') && Boolean(documentRevision);
  const drawerRef = useRef<HTMLDialogElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // 原生模态限制 Tab 焦点在抽屉内；只锁定背景滚动，正文仍可独立滚动。
    const drawer = drawerRef.current;
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    drawer?.showModal();
    document.body.style.overflow = 'hidden';
    return () => {
      drawer?.close();
      document.body.style.overflow = previousOverflow;
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, []);

  useEffect(() => {
    let active = true;
    void gatewayRequest<PageEnvelope<DocumentChunk>>(
      session,
      `v1/management/documents/${encodeURIComponent(documentId)}/chunks?page=${query.page}&page_size=20${documentRevision ? `&document_revision=${documentRevision}` : ''}`,
    ).then(
      (result) => { if (active) { setMeta(result.meta); setState({ status: 'ready', result }); } },
      (error: unknown) => { if (active) setState({ status: 'error', message: errorMessage(error) }); },
    );
    // 只丢弃过期结果，不修改后端任务；切换身份/文档时由父组件重新挂载。
    return () => { active = false; };
  }, [documentId, documentRevision, query, session]);

  /** 翻页/重试只清空正文并回到顶部；保留总数与固定页脚，同页重试也会发起请求。 */
  function loadPage(page: number) {
    if (listRef.current) listRef.current.scrollTop = 0;
    setSelectedChunk(null);
    setState({ status: 'loading' });
    setQuery({ page });
  }

  const totalPages = Math.max(1, Math.ceil(meta.total / meta.page_size));
  return (
    <dialog ref={drawerRef} className={`chunk-drawer${hasSource ? ' with-source' : ''}`} aria-labelledby="chunk-drawer-title"
      onCancel={(event) => { event.preventDefault(); onClose(); }}
      onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <header>
        <div><span>文档内容</span><h2 id="chunk-drawer-title" title={filename}>{filename}</h2><p>{meta.total} 个内容片段</p></div>
        <button aria-label="关闭内容片段" onClick={onClose}><X size={19} /></button>
      </header>
      <div className="chunk-workbench">
      {hasSource && documentRevision && <PdfSourcePreview session={session} documentId={documentId} revision={documentRevision}
        selectedChunk={selectedChunk} page={sourcePage} onPageChange={setSourcePage} />}
      <section className="chunk-column" aria-label="文档切块">
      <div ref={listRef} className="chunk-list" aria-busy={state.status === 'loading'}>
        {filename.toLowerCase().endsWith('.pdf') && !documentRevision && <p className="pdf-mapping-note">缺少文档修订信息，请刷新文档列表后预览原文件。</p>}
        {state.status === 'loading' && <div className="chunk-message" role="status"><RefreshCw className="spin" size={20} />正在读取内容…</div>}
        {state.status === 'error' && <div className="chunk-error"><p role="alert"><AlertCircle size={17} />无法加载切块：{state.message}</p><button className="secondary-button" onClick={() => loadPage(query.page)}>重试</button></div>}
        {state.status === 'ready' && (state.result.data.length ? state.result.data.map((chunk, index) => {
        const pages = sourcePages(chunk.positions);
        return (
          <article className={`chunk-card${selectedChunk?.id === chunk.id ? ' selected' : ''}`} key={chunk.id}>
            <header>
              <strong>片段 {(meta.page - 1) * meta.page_size + index + 1}</strong>
              <span>{pages.length > 0 ? `第 ${pages.join('、')} 页` : '无页码信息'}</span>
              {hasSource && <button className="chunk-locate" aria-pressed={selectedChunk?.id === chunk.id} onClick={() => {
                // 一块可跨多页；先跳到首个来源页，其余页可在原文件面板逐个定位。
                setSelectedChunk(chunk);
                if (pages.length) setSourcePage(pages[0]);
              }}>查看原文</button>}
            </header>
            <div className="chunk-content">{chunk.content || '该切块无文本内容。'}</div>
          </article>
        );
        }) : <div className="chunk-empty" role="status"><Layers3 size={30} /><h3>没有可显示的内容片段</h3></div>)}
      </div>
      {meta.total > meta.page_size && <footer className="chunk-pagination" aria-label="切块分页">
        <button disabled={state.status !== 'ready' || query.page <= 1} onClick={() => loadPage(query.page - 1)}><ChevronLeft size={16} />上一页</button>
        <span>{query.page} / {totalPages}</span>
        <button disabled={state.status !== 'ready' || query.page >= totalPages} onClick={() => loadPage(query.page + 1)}>下一页<ChevronRight size={16} /></button>
      </footer>}
      </section>
      </div>
    </dialog>
  );
}

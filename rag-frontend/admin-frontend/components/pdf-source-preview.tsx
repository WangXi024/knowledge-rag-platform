'use client';

/**
 * 原文件阅读面板：仅从带当前身份/修订号的同源只读接口加载 PDF，使用本地 PDF.js 渲染。
 * 每次只绘制一页；定位来自切块坐标，缺失或异常不猜测。关闭会取消请求并销毁阅读器。
 * 不执行 PDF 脚本、不提供外部链接/附件执行入口，也不向第三方发送文档。
 */
import { ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from 'pdfjs-dist';
import type { AdminSession, DocumentChunk } from '@/lib/contracts';
import { sourcePages, sourceRects } from '@/lib/pdf-positions';

/** session 与 revision 固定本次阅读范围，选中切块控制跳页；加载错误仅影响原文侧。 */
export function PdfSourcePreview({ session, documentId, revision, selectedChunk, page, onPageChange }: {
  session: AdminSession; documentId: string; revision: number; selectedChunk: DocumentChunk | null;
  page: number; onPageChange: (page: number) => void;
}) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let task: PDFDocumentLoadingTask | undefined;
    // 先完整接收经后端长度/摘要校验的流，再交给阅读器；中途断开不会显示不完整文件。
    void (async () => {
      const response = await fetch(`/api/gateway/v1/management/documents/${encodeURIComponent(documentId)}/source?document_revision=${revision}`, {
        headers: { 'X-Tenant-ID': session.tenantId, 'X-User-ID': session.userId, accept: 'application/pdf' },
        cache: 'no-store', signal: controller.signal,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null) as { error?: { message?: string } } | null;
        throw new Error(payload?.error?.message || `原文件加载失败（HTTP ${response.status}）`);
      }
      if (!response.headers.get('content-type')?.includes('application/pdf')) throw new Error('服务器未返回 PDF，请确认后端已更新。');
      const data = new Uint8Array(await response.arrayBuffer());
      const pdfjs = await import('pdfjs-dist');
      if (controller.signal.aborted) return;
      const resources = `/pdfjs/${pdfjs.version}/`;
      pdfjs.GlobalWorkerOptions.workerSrc = `${resources}pdf.worker.min.mjs`;
      task = pdfjs.getDocument({ data, cMapUrl: `${resources}cmaps/`, cMapPacked: true,
        standardFontDataUrl: `${resources}standard_fonts/`, wasmUrl: `${resources}wasm/`, useSystemFonts: true });
      const document = await task.promise;
      if (!controller.signal.aborted) setPdf(document);
    })().catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : '原文件加载失败');
    });
    return () => { controller.abort(); void task?.destroy(); };
  }, [session, documentId, revision, attempt]);

  const pages = sourcePages(selectedChunk?.positions ?? []);
  const validPage = pdf && page >= 1 && page <= pdf.numPages;
  return <section className="pdf-source" aria-label="PDF 原文件预览">
    <div className="pdf-toolbar"><strong>源文件</strong>
      <div><button aria-label="原文件上一页" disabled={!pdf || page <= 1} onClick={() => onPageChange(page - 1)}><ChevronLeft size={16} /></button>
        <span>{pdf ? `${page} / ${pdf.numPages}` : '正在加载'}</span>
        <button aria-label="原文件下一页" disabled={!pdf || page >= pdf.numPages} onClick={() => onPageChange(page + 1)}><ChevronRight size={16} /></button></div>
    </div>
    {selectedChunk && <div className="pdf-location" role="status">
      {pages.length ? <>对应页：{pages.map((number) => <button key={number} disabled={Boolean(pdf && number > pdf.numPages)} aria-pressed={page === number} onClick={() => onPageChange(number)}>{number}</button>)}</> : '此切块没有页码信息，无法定位原文。'}
    </div>}
    <div className="pdf-scroll">
      {error ? <div className="chunk-error"><p role="alert">{error}</p><button className="secondary-button" onClick={() => { setError(''); setPdf(null); setAttempt(attempt + 1); }}>重新加载原文件</button></div>
        : !pdf ? <div className="chunk-message" role="status"><RefreshCw className="spin" size={20} />正在加载原文件…</div>
          : validPage ? <PdfPage key={`${page}-${attempt}`} pdf={pdf} page={page} selectedChunk={selectedChunk} />
            : <p role="status">此切块页码超出原文件范围，无法定位。<button onClick={() => onPageChange(1)}>回到第一页</button></p>}
    </div>
  </section>;
}

/** 单页画布随容器缩放；页切换销毁旧画布/渲染任务，避免异步结果覆盖新页。 */
function PdfPage({ pdf, page, selectedChunk }: { pdf: PDFDocumentProxy; page: number; selectedChunk: DocumentChunk | null }) {
  const container = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(600);
  const [size, setSize] = useState({ width: 0, height: 0, mappable: false });
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(1, entry.contentRect.width)));
    if (container.current) observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let active = true;
    let render: RenderTask | undefined;
    void pdf.getPage(page).then(async (source) => {
      if (!active || !canvas.current) return;
      const natural = source.getViewport({ scale: 1 });
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      // 异常超长页面也限制画布像素，避免一个 PDF 页耗尽浏览器内存。
      const scale = Math.min(width / natural.width, Math.sqrt(16_000_000 / (natural.width * natural.height * ratio ** 2)));
      const viewport = source.getViewport({ scale });
      canvas.current.width = Math.floor(viewport.width * ratio);
      canvas.current.height = Math.floor(viewport.height * ratio);
      canvas.current.style.width = `${viewport.width}px`;
      canvas.current.style.height = `${viewport.height}px`;
      render = source.render({ canvas: canvas.current, viewport, transform: [ratio, 0, 0, ratio, 0, 0] });
      await render.promise;
      if (active) setSize({ width: natural.width, height: natural.height,
        // 裁切/旋转页面需额外转换契约；暂只跳页，不能画一个看似准确的错误框。
        mappable: source.rotate === 0 && source.view[0] === 0 && source.view[1] === 0 });
    }).catch((failure: unknown) => {
      if (active) setError(failure instanceof Error ? failure.message : '页面渲染失败');
    });
    return () => { active = false; render?.cancel(); };
  }, [pdf, page, width, attempt]);
  const rectangles = size.mappable ? sourceRects(selectedChunk?.positions ?? [], page, size.width, size.height) : [];
  const pageMatches = sourcePages(selectedChunk?.positions ?? []).includes(page);
  return <div ref={container} className="pdf-page-container">
    {error && <div className="chunk-error"><p role="alert">页面渲染失败：{error}</p>
      {/* 只重绘当前页，不重新下载文件或触发文档解析。 */}
      <button onClick={() => { setError(''); setAttempt(attempt + 1); }}>重试本页</button></div>}
    {selectedChunk && pageMatches && size.width > 0 && <p className="pdf-mapping-note" role="status">{rectangles.length ? `已标出 ${rectangles.length} 处对应区域` : '已定位到本页；缺少有效坐标或页面需特殊转换，暂不高亮。'}</p>}
    <div className="pdf-page"><canvas ref={canvas} aria-label={`原文件第 ${page} 页`} />
      <div className="pdf-highlights" aria-hidden="true">{rectangles.map((rect, index) => <span key={index} style={{ left: `${rect.left}%`, top: `${rect.top}%`, width: `${rect.width}%`, height: `${rect.height}%` }} />)}</div>
    </div>
  </div>;
}

"use client";

/**
 * 用户聊天主界面：管理会话历史、SSE 增量答案、查询规划、引用证据抽屉与会话删除。
 * 服务端始终限定当前 Tenant/User 可访问的租户共享和系统共享知识库，客户端只负责
 * 展示与交互状态，不选择或上传知识库。
 */

import { BookOpen, ChevronLeft, ChevronRight, Clock3, FileSearch, History, Languages, MessageSquarePlus, PanelRightClose, PanelRightOpen, Send, Sparkles, Square, Trash2 } from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { CacheType, Candidate, ChatMessage, Citation, ConversationSummary, EvidenceBundle } from "@/lib/ui-types";

/**
 * 单个浏览器页面最多允许两个会话同时生成答案。
 *
 * Gateway 虽然允许更多并发，但用户端一次问答还会同时发起“证据检索”和“流式回答”
 * 两条请求。把单用户并发限制为两个，可以让用户切换会话的同时继续后台生成，又不
 * 会让资源有限的本地或远端模型端点因瞬时请求过多而长时间排队。
 */
const MAX_PARALLEL_ANSWERS = 2;
const DRAFT_CONVERSATION_PREFIX = "draft:";

/**
 * 每个会话独立保存的界面状态。
 *
 * 旧实现把消息、问题、引用和错误都放在一组全局变量里，因此流式回答期间一旦切换
 * 会话，后续 SSE 文本就可能写进错误的窗口。现在这些数据以会话 Key 为边界隔离：
 * 服务端已有会话直接使用 UUID；尚未落库的新会话使用 ``draft:`` 开头的临时 Key。
 */
type ConversationViewState = {
  messages: ChatMessage[];
  question: string;
  evidence: EvidenceBundle;
  error: string;
  loading: boolean;
  hydrated: boolean;
};

/**
 * 浏览器内正在运行的回答任务。
 *
 * Controller 只取消所属会话的检索和 SSE，不会误伤其他会话；assistantId 用来确保
 * 增量文本只更新本次回答创建的占位消息。
 */
type ConversationRun = {
  controller: AbortController;
  assistantId: string;
};

function newConversationView(): ConversationViewState {
  return {
    messages: [],
    question: "",
    evidence: emptyEvidence(),
    error: "",
    loading: false,
    hydrated: false,
  };
}

function newDraftConversationKey(): string {
  return `${DRAFT_CONVERSATION_PREFIX}${crypto.randomUUID()}`;
}

function isDraftConversation(key: string): boolean {
  return key.startsWith(DRAFT_CONVERSATION_PREFIX);
}

function titleFromQuestion(question: string): string {
  return question.replace(/\s+/gu, " ").trim().slice(0, 42) || "新对话";
}

function normalize(value: string): string {
  return value.toLowerCase().replace(/\s+/gu, " ").replace(/[^\p{L}\p{N} ]/gu, "").trim();
}

function emptyEvidence(): EvidenceBundle {
  return { candidates: [], citations: [], durationMs: null, total: 0, cacheType: null };
}

function reconcileEvidence(
  candidates: Candidate[],
  citations: Citation[],
  durationMs: number | null,
  cacheType: CacheType | null,
): EvidenceBundle {
  // 将 retrieve 候选与 answer 引用按文档/块关联，形成可翻页且不重复的证据集合。
  const resolved = candidates.map((candidate) => ({ ...candidate }));
  for (const citation of citations) {
    const quote = normalize(citation.quote);
    const match = resolved.find((candidate) => {
      if (candidate.citationLabel || candidate.documentId !== citation.document_id) return false;
      const content = normalize(candidate.content);
      const needle = quote.slice(0, Math.min(80, quote.length));
      return needle.length >= 24 ? content.includes(needle) || quote.includes(content.slice(0, Math.min(80, content.length))) : false;
    });
    if (match) {
      match.citationLabel = citation.label;
    } else {
      resolved.push({
        chunkId: `citation-${citation.citation_id}`,
        documentId: citation.document_id,
        documentName: citation.document_name,
        headingPath: [],
        content: citation.quote,
        page: citation.page_start ?? null,
        score: citation.score ?? 0,
        vectorScore: null,
        termScore: null,
        // 旧回答只有 Citation、没有完整检索候选时无法证明执行过重排，因此保持为空，
        // 右侧栏不会为这类历史数据错误显示“已智能重排”。
        rerankScore: null,
        citationLabel: citation.label,
      });
    }
  }
  return { candidates: resolved, citations, durationMs, total: candidates.length, cacheType };
}

function score(value: number | null): string {
  return value === null || !Number.isFinite(value) ? "—" : value.toFixed(4);
}

function cacheSourceLabel(cacheType: CacheType | null | undefined): string {
  if (cacheType === "exact") return "命中精准缓存";
  if (cacheType === "semantic") return "命中语义缓存";
  if (cacheType === "none") return "直接问答";
  return "缓存状态未记录";
}

/**
 * 把回答中的 ``[C1]`` 引用标记转换为仅供界面识别的 Markdown 内部链接。
 *
 * React Markdown 随后会把这种链接交给自定义 ``a`` 渲染器，最终仍显示为原来的绿色
 * 引用按钮。代码围栏内的 ``[C1]`` 只是代码示例，不应该打开证据栏，所以逐行识别
 * ````` 或 ``~~~`` 围栏并跳过替换；已经带 URL 的标准 Markdown 链接也不会被改写。
 * 该转换只影响浏览器展示，不修改数据库保存的原始回答，也不会改变引用校验结果。
 */
function markdownWithCitationLinks(content: string): string {
  let fence: "```" | "~~~" | null = null;
  return content.split("\n").map((line) => {
    const marker = line.trimStart().match(/^(```|~~~)/u)?.[1] as "```" | "~~~" | undefined;
    if (marker) {
      fence = fence === null ? marker : fence === marker ? null : fence;
      return line;
    }
    if (fence !== null) return line;
    return line.replace(/\[(C\d+)\](?!\()/gu, "[$1](#rag-citation-$1)");
  }).join("\n");
}

/**
 * Markdown 元素映射同时承担两项安全职责。
 *
 * 第一，只有 ``#rag-citation-C数字`` 这种内部链接会变成可点击引用按钮，普通链接仍
 * 由浏览器按链接处理。第二，模型输出的 Markdown 图片不会直接加载远程 URL，避免
 * 文档中的提示词诱导浏览器访问跟踪地址；界面只展示图片替代文字。原始 HTML 还会
 * 由 ``skipHtml`` 完全忽略，因此回答不能注入脚本、iframe 或任意 DOM。
 */
const markdownComponents: Components = {
  a: ({ href, children }) => {
    const citation = href?.match(/^#rag-citation-(C\d+)$/u)?.[1];
    if (citation) {
      return <button type="button" className="citation-chip" data-citation={citation}>{children}</button>;
    }
    const external = href?.startsWith("http://") || href?.startsWith("https://");
    return <a href={href} target={external ? "_blank" : undefined} rel={external ? "noreferrer noopener" : undefined}>{children}</a>;
  },
  img: ({ alt }) => <span className="markdown-image-alt" role="img" aria-label={alt || "回答中的图片"}>图片：{alt || "未提供说明"}</span>,
};

/**
 * 将模型回答渲染为排版后的 Markdown，并把引用按钮点击事件转交给聊天主界面。
 * ``react-markdown`` 不使用 ``dangerouslySetInnerHTML``；GFM 插件只增加表格、删除线和
 * 任务列表等常见语法。点击引用时从按钮的 ``data-citation`` 读取 C 编号，不会执行
 * 模型生成的 JavaScript，也不会把回答正文发送到新的服务。
 */
function AnswerContent({ content, onCitation }: { content: string; onCitation: (label: string) => void }) {
  return (
    <div
      className="answer-copy"
      onClick={(event) => {
        const target = event.target;
        if (!(target instanceof HTMLElement)) return;
        const label = target.closest<HTMLElement>("[data-citation]")?.dataset.citation;
        if (label) onCitation(label);
      }}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents} skipHtml>
        {markdownWithCitationLinks(content)}
      </ReactMarkdown>
    </div>
  );
}

function parseHistoryMessages(data: unknown): ChatMessage[] {
  if (!Array.isArray(data)) return [];
  return data.map((item) => {
    const row = item as Record<string, unknown>;
    return {
      id: String(row.id),
      role: row.role === "assistant" ? "assistant" : "user",
      content: String(row.content || ""),
      evidence: row.role === "assistant" && row.evidence && typeof row.evidence === "object"
        ? row.evidence as EvidenceBundle
        : undefined,
      requestId: row.requestId ? String(row.requestId) : null,
    };
  });
}

export function ChatView() {
  const [initialConversationKey] = useState(newDraftConversationKey);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [draftConversations, setDraftConversations] = useState<ConversationSummary[]>([]);
  const [activeConversationKey, setActiveConversationKey] = useState(initialConversationKey);
  const [conversationViews, setConversationViews] = useState<Record<string, ConversationViewState>>(
    () => ({ [initialConversationKey]: newConversationView() }),
  );
  const [runningConversationKeys, setRunningConversationKeys] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [responseLanguage, setResponseLanguage] = useState<"zh-CN" | "en-US">("zh-CN");
  const [activeEvidence, setActiveEvidence] = useState<string>();
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(true);
  const runsRef = useRef<Map<string, ConversationRun>>(new Map());
  const bottomRef = useRef<HTMLDivElement>(null);

  /**
   * 只修改指定会话的局部状态，其他正在流式生成的会话保持原样。
   * 所有 SSE 回调都必须通过此入口按 Key 写入，不能重新引入页面级 messages/evidence。
   */
  const updateConversationView = useCallback((
    key: string,
    updater: (current: ConversationViewState) => ConversationViewState,
  ) => {
    setConversationViews((current) => ({
      ...current,
      [key]: updater(current[key] ?? newConversationView()),
    }));
  }, []);

  const loadConversations = useCallback(async () => {
    const response = await fetch("/api/conversations", { cache: "no-store" });
    if (response.status === 401) { window.location.href = "/login"; return; }
    if (!response.ok) return;
    const payload = await response.json() as { data?: ConversationSummary[] };
    setConversations(payload.data ?? []);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadConversations(), 0);
    return () => window.clearTimeout(timer);
  }, [loadConversations]);

  const activeView = conversationViews[activeConversationKey] ?? newConversationView();
  const messages = activeView.messages;
  const question = activeView.question;
  const evidence = activeView.evidence;
  const error = activeView.error;
  const activeConversationRunning = runningConversationKeys.has(activeConversationKey);
  const parallelLimitReached = runningConversationKeys.size >= MAX_PARALLEL_ANSWERS;

  const visibleConversations = useMemo(
    () => [...draftConversations, ...conversations]
      .sort((left, right) => right.updatedAt - left.updatedAt),
    [conversations, draftConversations],
  );

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [activeConversationKey, messages]);

  useEffect(() => () => {
    // 离开整个聊天模块时取消仍在浏览器中持有的请求，避免卸载后的组件继续写状态。
    for (const run of runsRef.current.values()) run.controller.abort();
    runsRef.current.clear();
  }, []);

  async function selectConversation(id: string) {
    // 切换仅改变当前展示对象；目标会话若仍在生成，SSE 会继续更新它自己的状态槽。
    setActiveConversationKey(id);
    setActiveEvidence(undefined);
    setEvidenceOpen(false);
    const cached = conversationViews[id];
    if (cached?.hydrated || runsRef.current.has(id) || isDraftConversation(id)) return;

    updateConversationView(id, (current) => ({ ...current, loading: true, error: "" }));
    try {
      const response = await fetch(`/api/conversations?id=${encodeURIComponent(id)}`, { cache: "no-store" });
      if (response.status === 401) { window.location.href = "/login"; return; }
      if (!response.ok) throw new Error("无法读取这段对话");
      const payload = await response.json() as { data?: { messages?: unknown[] } };
      const loaded = parseHistoryMessages(payload.data?.messages);
      const lastAssistant = [...loaded].reverse().find((item) => item.role === "assistant" && item.evidence);
      updateConversationView(id, (current) => ({
        ...current,
        messages: loaded,
        evidence: lastAssistant?.evidence ?? emptyEvidence(),
        error: "",
        loading: false,
        hydrated: true,
      }));
    } catch (caught) {
      updateConversationView(id, (current) => ({
        ...current,
        loading: false,
        error: caught instanceof Error ? caught.message : "无法读取这段对话",
      }));
    }
  }

  function newConversation() {
    const key = newDraftConversationKey();
    setConversationViews((current) => {
      const next = { ...current };
      const previous = next[activeConversationKey];
      // 未发送过问题的旧草稿没有历史价值，切走时顺便从内存清理。
      if (
        isDraftConversation(activeConversationKey)
        && !runsRef.current.has(activeConversationKey)
        && !previous?.messages.length
      ) delete next[activeConversationKey];
      next[key] = newConversationView();
      return next;
    });
    setActiveConversationKey(key);
    setActiveEvidence(undefined);
    setEvidenceOpen(false);
  }

  function stopConversation(key: string) {
    // AbortSignal 会沿用户端 BFF 传播到 Gateway；只终止这个 Key 对应的两条请求。
    runsRef.current.get(key)?.controller.abort();
  }

  async function deleteConversation(id: string) {
    if (!window.confirm("确认删除这段对话吗？")) return;
    stopConversation(id);
    if (isDraftConversation(id)) {
      setDraftConversations((current) => current.filter((item) => item.id !== id));
      setConversationViews((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
      if (activeConversationKey === id) newConversation();
      return;
    }
    await fetch(`/api/conversations?id=${encodeURIComponent(id)}`, { method: "DELETE" });
    setConversationViews((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
    if (activeConversationKey === id) newConversation();
    await loadConversations();
  }

  function showCitation(label: string, bundle?: EvidenceBundle) {
    if (bundle) {
      updateConversationView(activeConversationKey, (current) => ({ ...current, evidence: bundle }));
    }
    setEvidenceOpen(true);
    setActiveEvidence(label);
    window.setTimeout(() => document.getElementById(`evidence-${label}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 80);
  }

  function registerRun(key: string, run: ConversationRun) {
    runsRef.current.set(key, run);
    setRunningConversationKeys((current) => {
      const next = new Set(current);
      next.add(key);
      return next;
    });
  }

  function unregisterRun(key: string) {
    runsRef.current.delete(key);
    setRunningConversationKeys((current) => {
      const next = new Set(current);
      next.delete(key);
      return next;
    });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const query = question.trim();
    const runKey = activeConversationKey;
    // 历史消息尚未加载完成时不能追加新问题，否则稍后返回的历史响应会覆盖刚创建的
    // 流式消息。这个前置判断把“加载旧消息”和“追加新一轮”严格串行在同一会话内；
    // 它不会影响其他已经加载完成的会话并行回答。
    if (!query || activeView.loading || runsRef.current.has(runKey)) return;
    if (runsRef.current.size >= MAX_PARALLEL_ANSWERS) {
      updateConversationView(runKey, (current) => ({
        ...current,
        error: "已有两个会话正在回答，请等待其中一个完成或先停止一个会话。",
      }));
      return;
    }

    setEvidenceOpen(false);
    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: "user", content: query };
    const assistantId = crypto.randomUUID();
    updateConversationView(runKey, (current) => ({
      ...current,
      question: "",
      error: "",
      evidence: emptyEvidence(),
      hydrated: true,
      messages: [
        ...current.messages,
        userMessage,
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ],
    }));

    if (isDraftConversation(runKey)) {
      const now = Date.now();
      setDraftConversations((current) => current.some((item) => item.id === runKey)
        ? current
        : [{ id: runKey, title: titleFromQuestion(query), createdAt: now, updatedAt: now }, ...current]);
    }

    const controller = new AbortController();
    registerRun(runKey, { controller, assistantId });
    const languageForRun = responseLanguage;

    // 右侧证据和回答正文并行请求，任何一条失败都不会产生未处理的 Promise rejection。
    const retrievalPromise = fetch("/api/chat/retrieve", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query }), signal: controller.signal,
    }).then(async (response) => {
      const payload = await response.json() as { data?: { candidates?: Candidate[]; durationMs?: number; total?: number }; error?: string };
      if (!response.ok) throw new Error(payload.error || "检索失败");
      return payload.data ?? { candidates: [], durationMs: null, total: 0 };
    }).catch(() => ({ candidates: [] as Candidate[], durationMs: null, total: 0 }));

    let answer = "";
    let requestId = "";
    let cacheType: CacheType | null = null;
    const citations: Citation[] = [];
    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ query, messages: [], responseLanguage: languageForRun }),
        signal: controller.signal,
      });
      if (response.status === 401) { window.location.href = "/login"; return; }
      if (!response.ok || !response.body) {
        const payload = await response.json() as { error?: string };
        throw new Error(payload.error || "暂时无法生成回答");
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
        const blocks = buffer.split(/\r?\n\r?\n/u);
        buffer = blocks.pop() ?? "";
        for (const block of blocks) {
          const eventName = block.split(/\r?\n/u).find((line) => line.startsWith("event:"))?.slice(6).trim() || "message";
          const dataText = block.split(/\r?\n/u).filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
          if (!dataText) continue;
          const data = JSON.parse(dataText) as Record<string, unknown>;
          if (data.request_id) requestId = String(data.request_id);
          if (eventName === "answer.delta" && data.delta) {
            answer += String(data.delta);
            updateConversationView(runKey, (current) => ({
              ...current,
              messages: current.messages.map((item) => item.id === assistantId
                ? { ...item, content: answer }
                : item),
            }));
          }
          if (eventName === "citation" && data.citation) citations.push(data.citation as Citation);
          if (
            eventName === "completed"
            && (data.cache_type === "none" || data.cache_type === "exact" || data.cache_type === "semantic")
          ) {
            cacheType = data.cache_type;
          }
          if (eventName === "error") throw new Error(String((data.error as { message?: string } | undefined)?.message || "回答生成失败"));
        }
        if (done) break;
      }
      if (!answer.trim()) answer = languageForRun === "en-US" ? "I could not find enough evidence in your files." : "我暂时没有在你的文件中找到足够依据。";
      const retrieved = await retrievalPromise;
      const bundle = reconcileEvidence(
        retrieved.candidates ?? [],
        citations,
        retrieved.durationMs ?? null,
        cacheType,
      );
      updateConversationView(runKey, (current) => ({
        ...current,
        evidence: bundle,
        messages: current.messages.map((item) => item.id === assistantId
          ? { ...item, content: answer, evidence: bundle, requestId, streaming: false }
          : item),
      }));
      const saved = await fetch("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversationId: isDraftConversation(runKey) ? undefined : runKey,
          question: query,
          answer,
          evidence: bundle,
          requestId,
        }),
      });
      if (saved.ok) {
        const payload = await saved.json() as { data?: { conversationId?: string } };
        const savedConversationId = payload.data?.conversationId;
        if (savedConversationId && savedConversationId !== runKey) {
          // 新会话落库后把临时 Key 原子迁移到服务端 UUID；只在用户仍查看该草稿时切换。
          setConversationViews((current) => {
            const source = current[runKey];
            if (!source) return current;
            const next = { ...current, [savedConversationId]: source };
            delete next[runKey];
            return next;
          });
          setDraftConversations((current) => current.filter((item) => item.id !== runKey));
          setActiveConversationKey((current) => current === runKey ? savedConversationId : current);
        }
        await loadConversations();
      } else {
        updateConversationView(runKey, (current) => ({
          ...current,
          error: "回答已经完成，但本次会话记录保存失败。",
        }));
      }
    } catch (caught) {
      const cancelled = caught instanceof DOMException && caught.name === "AbortError";
      const message = cancelled ? "" : caught instanceof Error ? caught.message : "回答失败，请稍后重试";
      updateConversationView(runKey, (current) => ({
        ...current,
        error: message,
        messages: current.messages.map((item) => item.id === assistantId
          ? {
            ...item,
            content: answer.trim() || (cancelled ? "已停止生成。" : "这次回答没有完成，请稍后再试。"),
            streaming: false,
          }
          : item),
      }));
    } finally {
      unregisterRun(runKey);
    }
  }

  const documentCount = useMemo(() => new Set(evidence.candidates.map((item) => item.documentId)).size, [evidence]);

  return (
    <div className={`chat-page ${historyOpen ? "with-history" : ""} ${evidenceOpen ? "with-evidence" : ""}`}>
      <aside className={`history-panel ${historyOpen ? "open" : ""}`}>
        <div className="history-head"><strong><History size={16} /> 对话记录</strong><button onClick={() => setHistoryOpen(false)} aria-label="收起记录"><ChevronLeft size={16} /></button></div>
        <button className="new-chat" onClick={newConversation}><MessageSquarePlus size={17} /> 新对话</button>
        <div className="history-list">
          {visibleConversations.map((item) => {
            const running = runningConversationKeys.has(item.id);
            return (
            <div className={`history-item ${activeConversationKey === item.id ? "active" : ""} ${running ? "running" : ""}`} key={item.id}>
              <button onClick={() => void selectConversation(item.id)}>
                <span>{item.title}</span>
                <small>{running ? <span className="history-running"><i />正在回答，可切换查看</span> : new Date(item.updatedAt).toLocaleDateString("zh-CN")}</small>
              </button>
              {running
                ? <button className="history-stop" onClick={() => stopConversation(item.id)} aria-label="停止这段对话的回答"><Square size={12} /></button>
                : <button className="history-delete" onClick={() => void deleteConversation(item.id)} aria-label="删除对话"><Trash2 size={14} /></button>}
            </div>
            );
          })}
        </div>
      </aside>

      <section className="conversation-panel">
        <header className="conversation-head">
          <div className="conversation-title">
            {!historyOpen ? <button className="icon-action" onClick={() => setHistoryOpen(true)} title="展开对话记录"><ChevronRight size={17} /></button> : null}
            <span className="assistant-mark"><Sparkles size={18} /></span><div><strong>知识问答</strong><small>答案来自你上传的文件</small></div>
          </div>
          <div className="conversation-tools">
            <label><Languages size={15} /><select value={responseLanguage} onChange={(event) => setResponseLanguage(event.target.value as "zh-CN" | "en-US")}><option value="zh-CN">中文回答</option><option value="en-US">English</option></select></label>
            <button className="icon-action" onClick={() => setEvidenceOpen((value) => !value)} title={evidenceOpen ? "收起引用" : "展开引用"}>{evidenceOpen ? <PanelRightClose size={17} /> : <PanelRightOpen size={17} />}</button>
          </div>
        </header>

        <div className="message-list">
          {activeView.loading ? (
            <div className="chat-empty"><span className="loading-dot" /><h1>正在读取对话</h1><p>历史消息加载完成后会显示在这里。</p></div>
          ) : !messages.length ? (
            <div className="chat-empty"><span><BookOpen size={28} /></span><h1>从你的文档中找到答案</h1><p>提出问题，我会检索相关原文并标注引用来源。</p><div className="suggestions"><button onClick={() => updateConversationView(activeConversationKey, (current) => ({ ...current, question: "请总结我上传的文档内容" }))}>总结文档的主要内容</button><button onClick={() => updateConversationView(activeConversationKey, (current) => ({ ...current, question: "文档中最重要的结论是什么？" }))}>查找重要结论</button></div></div>
          ) : messages.map((message) => (
            <article className={`chat-message ${message.role}`} key={message.id}>
              <div className="message-avatar">{message.role === "user" ? "我" : <Sparkles size={16} />}</div>
              <div className="message-body">
                <small>{message.role === "user" ? "你" : "LazyTT"}</small>
                {message.role === "assistant" ? <AnswerContent content={message.content || "正在检索并组织答案…"} onCitation={(label) => showCitation(label, message.evidence)} /> : <div className="user-copy">{message.content}</div>}
                {message.streaming ? <span className="streaming-cursor" /> : null}
                {message.role === "assistant" && message.evidence?.citations.length ? (
                  <div className="answer-citations">{message.evidence.citations.map((citation) => <button key={citation.citation_id} onClick={() => showCitation(citation.label, message.evidence)}>{citation.label}</button>)}</div>
                ) : null}
              </div>
            </article>
          ))}
          <div ref={bottomRef} />
        </div>
        {error ? <div className="chat-error">{error}</div> : null}
        <form className="chat-composer-user" onSubmit={submit}>
          <textarea
            value={question}
            onChange={(event) => updateConversationView(activeConversationKey, (current) => ({ ...current, question: event.target.value }))}
            onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
            placeholder={activeView.loading
              ? "正在读取这段对话…"
              : activeConversationRunning
                ? "当前会话正在回答，可切换到其他会话继续提问…"
                : "向你的文档提问…"}
            rows={2}
            disabled={activeConversationRunning || activeView.loading}
          />
          <div>
            <span>{activeConversationRunning
              ? "回答会在后台继续生成，你可以切换会话"
              : parallelLimitReached
                ? "已有 2 个会话正在回答，请先等待或停止一个"
                : "Enter 发送 · Shift + Enter 换行"}</span>
            {activeConversationRunning
              ? <button type="button" className="send-button stop-button" onClick={() => stopConversation(activeConversationKey)} title="停止生成"><Square size={15} /></button>
              : <button className="send-button" disabled={activeView.loading || parallelLimitReached || !question.trim()}><Send size={18} /></button>}
          </div>
        </form>
      </section>

      <aside className={`evidence-panel ${evidenceOpen ? "open" : ""}`}>
        <div className="evidence-head"><div><span className="eyebrow">SOURCES</span><h2>检索与引用</h2></div><button className="icon-action" onClick={() => setEvidenceOpen(false)}><PanelRightClose size={17} /></button></div>
        <div className="cache-status" aria-label="回答缓存状态">
          <small><strong>回答来源</strong>{cacheSourceLabel(evidence.cacheType)}</small>
          <small>
            精准缓存：{evidence.cacheType === null || evidence.cacheType === undefined ? "未记录" : evidence.cacheType === "exact" ? "已命中" : "未命中"}
            <i aria-hidden="true">·</i>
            语义缓存：{evidence.cacheType === null || evidence.cacheType === undefined ? "未记录" : evidence.cacheType === "semantic" ? "已命中" : "未命中"}
          </small>
        </div>
        <div className="evidence-stats">
          <span><strong>{evidence.total}</strong> 检索片段</span><span><strong>{documentCount}</strong> 份文档</span><span><strong>{evidence.citations.length}</strong> 条引用</span>
          {evidence.durationMs !== null ? <span><Clock3 size={13} /><strong>{Math.round(evidence.durationMs)}</strong> ms</span> : null}
        </div>
        <div className="evidence-list">
          {evidence.candidates.length ? evidence.candidates.map((candidate, index) => {
            const label = candidate.citationLabel || String(index + 1);
            return (
              <article id={candidate.citationLabel ? `evidence-${candidate.citationLabel}` : undefined} className={`evidence-card ${candidate.citationLabel ? "cited" : ""} ${activeEvidence === candidate.citationLabel ? "active" : ""}`} key={candidate.chunkId}>
                <span className="evidence-index">{label}</span>
                <div className="evidence-content">
                  <div className="score-row">
                    <span>融合分 {score(candidate.score)}</span>
                    <span>关键词相似度 {score(candidate.termScore)}</span>
                    <span>向量相似度 {score(candidate.vectorScore)}</span>
                    {/* 只有真实分数存在时才告诉用户“已智能重排”，避免外部服务降级后误报。 */}
                    {candidate.rerankScore != null ? <span className="rerank-badge">已智能重排 · 重排相关度 {score(candidate.rerankScore)}</span> : null}
                  </div>
                  <h3>{candidate.documentName}{candidate.page ? <small>第 {candidate.page} 页</small> : null}</h3>
                  {candidate.headingPath.length ? <div className="heading-path">{candidate.headingPath.join(" / ")}</div> : null}
                  <p>{candidate.content}</p>
                </div>
              </article>
            );
          }) : (
            <div className="evidence-empty"><FileSearch size={30} /><h3>引用会显示在这里</h3><p>完成一次问答后，可查看所有检索片段、来源和评分。</p></div>
          )}
        </div>
      </aside>
    </div>
  );
}

#!/usr/bin/env python3
"""把运行时 OpenAPI 契约生成为可离线分享的中文单文件接口文档。

这个脚本只读取已经从 Gateway 下载的 ``docs/rag-service-openapi.json`` 和项目维护的
稳定错误码说明，不访问数据库、RAGFlow 或任何模型服务。生成结果把 OpenAPI 数据、
样式和交互脚本全部嵌入一个 HTML，因此收件人双击文件即可阅读，不需要安装依赖，
也不会因为后端停止运行而丢失接口参数和响应模型。

页面上的请求示例仅用于说明调用方式，不会从浏览器直接执行写操作。这样既适合对外
评审，又能避免误触删除、回滚或重新解析等高影响接口。
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "docs" / "rag-service-openapi.json"
ERRORS_PATH = REPO_ROOT / "rag-development-spec" / "docs" / "04-错误码说明.md"
OUTPUT_PATH = REPO_ROOT / "docs" / "rag-service-api.html"


# OpenAPI运行时摘要采用英文；这里提供面向使用者的中文标题和功能说明。没有列出的
# operationId仍会使用契约原文，保证新增接口不会因为缺少人工翻译而从文档中消失。
OPERATION_COPY: dict[str, tuple[str, str]] = {
    "getLiveness": ("进程存活检查", "确认RAG Gateway进程是否正在运行。"),
    "getReadiness": ("依赖就绪检查", "检查应用数据库、Redis、RAGFlow及必需模型依赖。"),
    "getVersion": ("查询服务版本", "返回API版本、构建信息和RAGFlow契约版本。"),
    "getCapabilities": ("查询系统能力", "返回文件格式、Profile、Rerank和功能开关。"),
    "listSharedKnowledgeBases": ("浏览共享知识库", "普通用户分页读取同租户共享库和系统共享库。"),
    "createKnowledgeBase": ("创建知识库", "创建租户共享或系统共享知识库。"),
    "listKnowledgeBases": ("查询知识库列表", "分页查询当前租户可访问的共享知识库。"),
    "getKnowledgeBase": ("查询知识库详情", "读取指定知识库的状态、Profile和文档数量。"),
    "updateKnowledgeBase": ("修改知识库", "修改名称、描述及未来任务使用的Profile。"),
    "deleteKnowledgeBase": ("删除知识库", "异步删除知识库；非空知识库需要显式force。"),
    "uploadDocument": ("上传文档", "上传单个文件，并可自动创建解析任务。"),
    "listSharedDocuments": ("浏览共享知识库文档", "普通用户只读分页查看可访问知识库中的文档。"),
    "getSharedDocument": ("浏览共享文档详情", "普通用户只读查看可访问共享文档的处理状态。"),
    "listSharedDocumentChunks": ("浏览共享文档Chunk", "普通用户只读分页查看共享文档正文和来源位置。"),
    "listDocuments": ("查询文档列表", "分页查询知识库中的文档及处理状态。"),
    "getDocument": ("查询文档详情", "查询文档Metadata、Profile、进度、Chunk和错误。"),
    "deleteDocument": ("删除文档", "异步删除文档、物理文件和索引。"),
    "uploadDocumentVersion": ("上传文档新版本", "解析候选版本，成功后安全切换活动版本。"),
    "listDocumentVersions": ("查询文档版本", "分页查看逻辑文档的版本历史。"),
    "rollbackDocumentVersion": ("回滚文档版本", "把仍保留物理索引的退役版本重新发布。"),
    "parseDocument": ("启动首次解析", "为已上传但未自动解析的文档创建解析任务。"),
    "reparseDocument": ("重新解析文档", "清理旧Chunk后使用指定Profile重建索引。"),
    "retryDocument": ("重试失败文档", "重新执行满足重试条件的失败文档任务。"),
    "listDocumentChunks": ("查询文档Chunk", "查看真实Chunk正文、页码、位置及Metadata。"),
    "getJob": ("查询异步任务", "查询任务状态、进度、尝试次数和失败原因。"),
    "cancelJob": ("取消异步任务", "请求取消尚未结束且支持取消的Job。"),
    "retrieve": ("执行混合检索", "执行QueryPlan、多路召回、RRF和Rerank，不调用回答LLM。"),
    "createAnswer": ("执行非流式问答", "检索知识、调用LLM并返回答案、引用和耗时。"),
    "streamAnswer": ("执行SSE流式问答", "以事件流返回Token、引用、完成或错误事件。"),
    "getRequest": ("查询问答请求", "查询问答状态、结果摘要、缓存类型和警告。"),
    "cancelRequest": ("取消问答请求", "取消正在检索或生成中的问答请求。"),
    "listRequestCitations": ("查询回答引用", "列出一次问答请求产生的全部引用。"),
    "getCitation": ("查询引用详情", "查看引用原文、位置、文档以及各项检索分数。"),
    "createFeedback": ("提交回答反馈", "提交点赞、点踩、纠错答案或引用反馈。"),
}


TAG_COPY: dict[str, tuple[str, str]] = {
    "System": ("系统状态", "健康、版本与运行能力"),
    "KnowledgeBases": ("知识库", "知识空间的创建、查询、修改与删除"),
    "Documents": ("文档与索引", "上传、解析、Chunk、版本更新与回滚"),
    "Jobs": ("异步任务", "查询解析进度与取消任务"),
    "Retrieval": ("混合检索", "QueryPlan、多路召回、融合与重排"),
    "Answers": ("RAG问答", "JSON/SSE回答、请求状态与取消"),
    "Citations": ("引用溯源", "回答引用列表、原文和位置"),
    "Feedback": ("质量反馈", "回答和引用质量反馈"),
}


def load_json(path: Path) -> dict:
    """读取并验证OpenAPI根对象，错误时给出明确文件位置。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "paths" not in payload:
        raise ValueError(f"{path} 不是有效的OpenAPI对象")
    return payload


def parse_error_catalog(path: Path) -> list[dict[str, str]]:
    """从错误码Markdown表格提取稳定错误码，供HTML按前缀搜索和筛选。"""

    entries: list[dict[str, str]] = []
    row_pattern = re.compile(
        r"^\|\s*`(?P<code>RAG-[A-Z]+-\d+)`\s*"
        r"\|\s*(?P<http>[^|]+?)\s*"
        r"\|\s*(?P<retryable>[^|]+?)\s*"
        r"\|\s*(?P<message>[^|]+?)\s*\|$"
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        match = row_pattern.match(line.strip())
        if match:
            entries.append({key: value.strip() for key, value in match.groupdict().items()})
    return entries


def json_for_script(value: object) -> str:
    """序列化为安全的内联JavaScript数据，避免内容意外结束script标签。"""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def build_html(spec: dict, error_catalog: list[dict[str, str]]) -> str:
    # 离线文档中的curl示例需要一个可直接复制的访问入口。默认使用本机已经设置的
    # Bonjour/mDNS名称；其他部署者可在重新生成文档时通过RAG_API_DOCS_BASE_URL
    # 覆盖它，而不必修改脚本源码。
    api_base_url = os.environ.get("RAG_API_DOCS_BASE_URL", "http://wx.local:8080").rstrip("/")
    api_base_url_html = html.escape(api_base_url)
    api_base_url_js = json.dumps(api_base_url, ensure_ascii=False)
    """生成完整HTML；接口和Schema的详细展开由浏览器端脚本完成。"""

    title = html.escape(spec.get("info", {}).get("title", "RAG Service API"))
    version = html.escape(spec.get("info", {}).get("version", "unknown"))
    spec_json = json_for_script(spec)
    errors_json = json_for_script(error_catalog)
    operation_copy_json = json_for_script(OPERATION_COPY)
    tag_copy_json = json_for_script(TAG_COPY)
    operation_count = sum(
        1
        for path_item in spec.get("paths", {}).values()
        for method in path_item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="LazyTT RAG Gateway中文接口文档，包含{operation_count}个接口、参数、响应和错误上下文。">
  <title>{title} · 中文接口文档</title>
  <style>
    :root {{
      --ink:#17211d; --muted:#66736d; --line:#dbe4df; --paper:#f5f8f6;
      --card:#ffffff; --brand:#087f68; --brand-dark:#065d4d; --soft:#e8f5f1;
      --get:#2563eb; --post:#078f6f; --patch:#9a6700; --delete:#d33b49;
      --shadow:0 18px 48px rgba(24,50,40,.08); --radius:18px;
    }}
    * {{ box-sizing:border-box; }}
    html {{ scroll-behavior:smooth; }}
    body {{ margin:0; color:var(--ink); background:var(--paper); font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
    button,input,select {{ font:inherit; }}
    code,pre,.path {{ font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace; }}
    a {{ color:var(--brand); text-decoration:none; }}
    .app {{ min-height:100vh; }}
    .hero {{ color:#fff; background:radial-gradient(circle at 78% 12%,rgba(43,210,166,.28),transparent 32%),linear-gradient(135deg,#092b24,#0a4d40 55%,#087f68); padding:64px max(28px,calc((100vw - 1440px)/2)); }}
    .eyebrow {{ font-size:12px; font-weight:800; letter-spacing:.16em; text-transform:uppercase; color:#89e6cd; }}
    h1 {{ max-width:900px; margin:14px 0 12px; font-size:clamp(38px,5vw,72px); line-height:1.02; letter-spacing:-.04em; }}
    .hero p {{ max-width:760px; margin:0; color:#cfe7df; font-size:18px; line-height:1.7; }}
    .hero-meta {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:28px; }}
    .pill {{ display:inline-flex; align-items:center; gap:7px; border:1px solid rgba(255,255,255,.18); border-radius:999px; padding:8px 12px; background:rgba(255,255,255,.09); font-size:13px; }}
    .layout {{ display:grid; grid-template-columns:260px minmax(0,1fr); max-width:1500px; margin:0 auto; }}
    aside {{ position:sticky; top:0; align-self:start; height:100vh; overflow:auto; padding:28px 18px; border-right:1px solid var(--line); background:rgba(245,248,246,.95); backdrop-filter:blur(14px); }}
    .nav-title {{ padding:0 10px 12px; font-size:12px; font-weight:800; color:var(--muted); letter-spacing:.12em; text-transform:uppercase; }}
    nav a {{ display:flex; justify-content:space-between; align-items:center; padding:10px; border-radius:10px; color:#33413b; font-size:14px; }}
    nav a:hover {{ background:#e5eeea; color:var(--brand-dark); }}
    nav .count {{ min-width:28px; text-align:center; border-radius:999px; padding:2px 7px; background:#dfe9e4; font-size:12px; }}
    main {{ min-width:0; padding:38px clamp(22px,4vw,64px) 80px; }}
    section {{ scroll-margin-top:24px; margin-bottom:46px; }}
    h2 {{ margin:0 0 10px; font-size:30px; letter-spacing:-.025em; }}
    h3 {{ margin:0; }}
    .lead {{ color:var(--muted); line-height:1.7; }}
    .stats {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin:24px 0; }}
    .stat,.card {{ border:1px solid var(--line); border-radius:var(--radius); background:var(--card); box-shadow:var(--shadow); }}
    .stat {{ padding:22px; }}
    .stat strong {{ display:block; font-size:32px; color:var(--brand-dark); }}
    .stat span {{ color:var(--muted); font-size:13px; }}
    .card {{ padding:24px; }}
    .notice {{ border-left:5px solid #d59a00; background:#fff8df; box-shadow:none; }}
    .notice strong {{ color:#714f00; }}
    .flow {{ display:grid; grid-template-columns:repeat(5,1fr); align-items:center; gap:9px; margin-top:20px; }}
    .flow-step {{ min-height:92px; display:flex; align-items:center; justify-content:center; text-align:center; border:1px solid #b9d9cf; border-radius:14px; padding:12px; background:var(--soft); font-weight:750; line-height:1.45; }}
    .arrow {{ text-align:center; color:var(--brand); font-size:22px; }}
    .address-grid,.module-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }}
    .address {{ padding:18px; border:1px solid var(--line); border-radius:14px; background:#fbfdfc; }}
    .address small {{ display:block; margin-bottom:7px; color:var(--muted); }}
    .address code {{ word-break:break-all; color:var(--brand-dark); }}
    .module-card {{ padding:18px; border:1px solid var(--line); border-radius:14px; background:#fff; }}
    .module-card b {{ display:block; margin-bottom:6px; }}
    .module-card p {{ margin:0; color:var(--muted); font-size:14px; line-height:1.55; }}
    .toolbar {{ position:sticky; top:12px; z-index:10; display:grid; grid-template-columns:minmax(220px,1fr) auto auto auto; gap:10px; margin:20px 0; padding:12px; border:1px solid var(--line); border-radius:16px; background:rgba(255,255,255,.94); box-shadow:var(--shadow); backdrop-filter:blur(16px); }}
    .toolbar input,.toolbar select,.toolbar button,.copy-button {{ border:1px solid #cfdad5; border-radius:10px; padding:10px 12px; color:var(--ink); background:#fff; }}
    .toolbar button,.copy-button {{ cursor:pointer; }}
    .toolbar button:hover,.copy-button:hover {{ border-color:var(--brand); color:var(--brand); }}
    .tag-head {{ display:flex; align-items:flex-end; justify-content:space-between; gap:16px; margin:38px 0 14px; padding-bottom:12px; border-bottom:1px solid var(--line); }}
    .tag-head p {{ margin:5px 0 0; color:var(--muted); }}
    .tag-count {{ color:var(--muted); font-size:13px; white-space:nowrap; }}
    details.endpoint {{ margin:11px 0; border:1px solid var(--line); border-radius:15px; background:#fff; overflow:hidden; box-shadow:0 7px 24px rgba(24,50,40,.045); }}
    details.endpoint[open] {{ border-color:#a8cfc3; box-shadow:var(--shadow); }}
    details.endpoint > summary {{ display:grid; grid-template-columns:82px minmax(240px,1.2fr) minmax(180px,.8fr); align-items:center; gap:15px; padding:16px; cursor:pointer; list-style:none; }}
    details.endpoint > summary::-webkit-details-marker {{ display:none; }}
    .method {{ display:inline-flex; align-items:center; justify-content:center; border-radius:8px; padding:8px 10px; color:#fff; font-weight:850; font-size:12px; letter-spacing:.04em; }}
    .GET {{ background:var(--get); }} .POST {{ background:var(--post); }} .PATCH {{ background:var(--patch); }} .DELETE {{ background:var(--delete); }}
    .path {{ overflow-wrap:anywhere; font-size:14px; font-weight:700; }}
    .summary-title {{ color:#4d5b55; font-size:14px; }}
    .endpoint-body {{ padding:6px 22px 24px; border-top:1px solid var(--line); }}
    .endpoint-desc {{ margin:18px 0; color:#42504a; line-height:1.7; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:8px; margin:12px 0 20px; }}
    .chip {{ border-radius:999px; padding:6px 10px; background:#edf3f0; color:#3d4c46; font-size:12px; font-weight:700; }}
    .chip.required {{ background:#fff0d2; color:#775100; }}
    .subsection {{ margin-top:24px; }}
    .subsection h4 {{ margin:0 0 10px; font-size:16px; }}
    .table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:12px; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th,td {{ padding:11px 12px; border-bottom:1px solid #e8eeeb; text-align:left; vertical-align:top; }}
    th {{ color:#56645e; background:#f5f8f6; font-size:12px; }}
    tr:last-child td {{ border-bottom:0; }}
    td code {{ color:#075f4f; overflow-wrap:anywhere; }}
    pre {{ position:relative; overflow:auto; margin:0; border-radius:12px; padding:18px; background:#10251f; color:#d8f3e9; line-height:1.65; font-size:12px; white-space:pre; }}
    .code-shell {{ position:relative; }}
    .copy-button {{ position:absolute; top:8px; right:8px; z-index:2; padding:6px 9px; border-color:#31574b; background:#17372e; color:#d8f3e9; font-size:11px; }}
    .response-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); align-items:start; gap:12px; }}
    details.response {{ overflow:hidden; border:1px solid var(--line); border-radius:12px; background:#fbfdfc; }}
    details.response[open] {{ grid-column:1/-1; border-color:#accfc4; background:#fff; }}
    details.response > summary {{ display:flex; align-items:center; gap:12px; min-height:74px; padding:14px; cursor:pointer; list-style:none; }}
    details.response > summary::-webkit-details-marker, details.response-part > summary::-webkit-details-marker {{ display:none; }}
    .response-summary-copy {{ min-width:0; flex:1; }}
    .response-summary-copy p {{ overflow:hidden; margin:4px 0 0; color:var(--muted); font-size:13px; line-height:1.45; text-overflow:ellipsis; white-space:nowrap; }}
    .response-chevron {{ color:#77857f; font-size:18px; transition:transform .2s ease; }}
    details.response[open] > summary .response-chevron, details.response-part[open] > summary .response-chevron {{ transform:rotate(180deg); }}
    .response-body {{ padding:0 14px 14px; border-top:1px solid var(--line); }}
    .response .status {{ display:inline-flex; flex:0 0 auto; border-radius:999px; padding:4px 8px; background:#e7f4ef; color:#086d59; font-weight:800; font-size:12px; }}
    .response.error .status {{ background:#ffebed; color:#ad2835; }}
    details.response-part {{ margin-top:12px; border:1px solid var(--line); border-radius:11px; background:#f8fbf9; }}
    details.response-part > summary {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 14px; cursor:pointer; list-style:none; font-size:13px; font-weight:800; }}
    .response-part-content {{ padding:0 12px 12px; }}
    .field-count {{ color:var(--muted); font-size:11px; font-weight:650; }}
    .runtime-note {{ margin:12px 0 0; border-left:4px solid #d59a00; border-radius:8px; padding:10px 12px; background:#fff8df; color:#6e4b00; font-size:12px; line-height:1.6; }}
    .error-groups {{ display:grid; gap:14px; }}
    .error-group {{ border:1px solid var(--line); border-radius:14px; background:#fff; overflow:hidden; }}
    .error-group h3 {{ padding:15px 17px; background:#f5f8f6; font-size:16px; }}
    .empty {{ display:none; padding:28px; border:1px dashed #afbeb7; border-radius:14px; text-align:center; color:var(--muted); }}
    footer {{ padding:28px; text-align:center; color:var(--muted); font-size:12px; }}
    @media (max-width:980px) {{
      .layout {{ display:block; }} aside {{ position:relative; width:100%; height:auto; border-right:0; border-bottom:1px solid var(--line); }} nav {{ display:flex; overflow:auto; }} nav a {{ flex:0 0 auto; }}
      .stats {{ grid-template-columns:repeat(2,1fr); }} .flow {{ grid-template-columns:1fr; }} .arrow {{ transform:rotate(90deg); }}
      details.endpoint > summary {{ grid-template-columns:72px 1fr; }} .summary-title {{ grid-column:2; }}
    }}
    @media (max-width:640px) {{
      .hero {{ padding:44px 22px; }} main {{ padding:28px 16px 60px; }} .stats,.address-grid,.module-grid,.response-grid {{ grid-template-columns:1fr; }}
      .toolbar {{ position:relative; top:auto; grid-template-columns:1fr 1fr; }} .toolbar input {{ grid-column:1/-1; }}
      details.endpoint > summary {{ grid-template-columns:66px 1fr; gap:10px; padding:13px; }}
    }}
    @media print {{
      aside,.toolbar,.copy-button {{ display:none!important; }} .layout {{ display:block; }} main {{ padding:20px; }} details.endpoint {{ break-inside:avoid; }} details.endpoint > * {{ display:block; }} .hero {{ background:#fff; color:#000; padding:20px; }} .hero p,.eyebrow {{ color:#333; }}
    }}
  </style>
</head>
<body>
  <div class="app">
    <header class="hero">
      <div class="eyebrow">LazyTT · Scope-isolated RAG Gateway</div>
      <h1>{title}<br>中文接口文档</h1>
      <p>面向MCP Server、业务后端和开发者的完整调用说明。覆盖接口功能、访问地址、Tenant/User隔离、输入参数、输出模型、错误类型和错误上下文。</p>
      <div class="hero-meta">
        <span class="pill">API v{version}</span>
        <span class="pill">OpenAPI {html.escape(spec.get('openapi', 'unknown'))}</span>
        <span class="pill" id="hero-operation-count">{operation_count}个接口</span>
        <span class="pill">单文件 · 离线可读</span>
      </div>
    </header>

    <div class="layout">
      <aside>
        <div class="nav-title">接口文档</div>
        <nav id="side-nav">
          <a href="#overview"><span>系统概览</span></a>
          <a href="#access"><span>接入规则</span></a>
          <a href="#modules"><span>功能模块</span></a>
          <a href="#interfaces"><span>全部接口</span><span class="count">{operation_count}</span></a>
          <a href="#responses"><span>响应格式</span></a>
          <a href="#errors"><span>错误类型</span></a>
          <a href="#mcp"><span>MCP调用</span></a>
        </nav>
      </aside>

      <main>
        <section id="overview">
          <h2>系统概览</h2>
          <p class="lead">外部调用方只访问RAG Gateway，不直接连接RAGFlow、数据库、Redis、Elasticsearch、MinIO或模型供应商。</p>
          <div class="stats">
            <div class="stat"><strong id="stat-operations">{operation_count}</strong><span>公开HTTP接口</span></div>
            <div class="stat"><strong id="stat-tags">8</strong><span>功能分组</span></div>
            <div class="stat"><strong id="stat-schemas">60</strong><span>请求与响应模型</span></div>
            <div class="stat"><strong id="stat-errors">0</strong><span>稳定错误码</span></div>
          </div>
          <div class="flow" aria-label="API调用链路">
            <div class="flow-step">Identity / MCP Server<br>取得稳定Tenant/User</div><div class="arrow">→</div>
            <div class="flow-step">RAG Gateway :8080<br>接收两个身份Header</div><div class="arrow">→</div>
            <div class="flow-step">派生security_scope_id<br>执行隔离后的RAG业务</div>
          </div>
        </section>

        <section id="access">
          <h2>接口访问与Tenant/User规则</h2>
          <div class="address-grid">
            <div class="address"><small>本机Gateway</small><code>http://127.0.0.1:8080</code></div>
            <div class="address"><small>局域网Gateway（mDNS）</small><code>{api_base_url_html}</code></div>
            <div class="address"><small>Swagger UI</small><code>{api_base_url_html}/docs</code></div>
            <div class="address"><small>OpenAPI JSON</small><code>{api_base_url_html}/openapi.json</code></div>
          </div>
          <div class="card notice" style="margin-top:16px">
            <strong>当前是Direct Tenant/User开发联调模式</strong>
            <p class="lead">除健康、版本和能力接口外，业务请求必须同时携带<code>X-Tenant-ID</code>和<code>X-User-ID</code>。Tenant ID决定可访问的租户共享范围，User ID保留调用主体、审计、缓存和幂等边界；两个Header都不会证明调用者真正拥有该身份，因此禁止把当前模式直接暴露到公网或不受信网络。</p>
          </div>
          <div class="card" style="margin-top:16px">
            <div class="table-wrap"><table>
              <thead><tr><th>Header</th><th>是否必填</th><th>用途</th><th>示例</th></tr></thead>
              <tbody>
                <tr><td><code>X-Tenant-ID</code></td><td>业务接口必填</td><td>确定TENANT共享范围；建议使用Identity不可变租户主键</td><td><code>identity-tenant-123</code></td></tr>
                <tr><td><code>X-User-ID</code></td><td>业务接口必填</td><td>标识调用主体，用于审计、缓存和幂等；不形成个人知识库范围</td><td><code>identity-user-123</code></td></tr>
                <tr><td><code>Idempotency-Key</code></td><td>部分写操作必填</td><td>防止网络重试重复创建资源或任务</td><td><code>create-kb-001</code></td></tr>
                <tr><td><code>X-Request-ID</code></td><td>可选</td><td>调用链追踪；不传时由Gateway生成</td><td><code>UUID</code></td></tr>
                <tr><td><code>Authorization</code></td><td>当前不使用</td><td>预留给未来Identity JWT + JWKS方案</td><td>—</td></tr>
              </tbody>
            </table></div>
          </div>
        </section>

        <section id="modules">
          <h2>功能模块</h2>
          <p class="lead">接口按业务职责分组，页面下方会从OpenAPI自动生成每个接口的参数和响应。</p>
          <div class="module-grid" id="module-grid"></div>
        </section>

        <section id="interfaces">
          <h2>全部接口</h2>
          <p class="lead">搜索接口路径、中文功能、operationId或模块名称。展开接口可以查看参数、请求体、curl示例和全部响应状态。</p>
          <div class="toolbar">
            <input id="search" type="search" placeholder="搜索路径、功能或字段…" aria-label="搜索接口">
            <select id="method-filter" aria-label="按Method筛选"><option value="ALL">全部Method</option><option>GET</option><option>POST</option><option>PATCH</option><option>DELETE</option></select>
            <button id="expand-all" type="button">展开全部</button>
            <button id="print-doc" type="button">打印 / PDF</button>
          </div>
          <div id="endpoint-groups"></div>
          <div id="empty" class="empty">没有找到匹配的接口。</div>
        </section>

        <section id="responses">
          <h2>统一Response格式</h2>
          <div class="response-grid">
            <div class="card"><h3>普通成功响应</h3><div class="subsection"><pre>{{
  "request_id": "UUID",
  "data": {{}}
}}</pre></div></div>
            <div class="card"><h3>分页成功响应</h3><div class="subsection"><pre>{{
  "request_id": "UUID",
  "data": [],
  "meta": {{
    "page": 1,
    "page_size": 20,
    "total": 0
  }}
}}</pre></div></div>
            <div class="card"><h3>HTTP错误响应</h3><div class="subsection"><pre>{{
  "request_id": "UUID",
  "error": {{
    "code": "RAG-CONFIG-001",
    "message": "配置 Profile 不存在",
    "retryable": false,
    "details": []
  }}
}}</pre></div></div>
            <div class="card"><h3>SSE错误事件</h3><div class="subsection"><pre>event: error
data: {{
  "request_id": "UUID",
  "sequence": 9,
  "error": {{
    "code": "RAG-LLM-002",
    "message": "生成超时",
    "retryable": true
  }}
}}</pre></div></div>
          </div>
        </section>

        <section id="errors">
          <h2>错误类型与错误上下文</h2>
          <p class="lead"><code>error.code</code>是程序判断依据；<code>retryable</code>决定是否有限重试；<code>details</code>提供字段名和失败原因；中文<code>message</code>只用于展示。</p>
          <div class="card" style="margin-bottom:16px">
            <div class="table-wrap"><table>
              <thead><tr><th>字段</th><th>含义</th><th>调用方处理</th></tr></thead>
              <tbody>
                <tr><td><code>request_id</code></td><td>本次调用追踪UUID</td><td>日志和缺陷单必须保留</td></tr>
                <tr><td><code>error.code</code></td><td>稳定机器错误码</td><td>使用该字段分支处理</td></tr>
                <tr><td><code>error.message</code></td><td>面向人的错误描述</td><td>可以展示，不能作为程序判断条件</td></tr>
                <tr><td><code>error.retryable</code></td><td>稍后重试是否可能成功</td><td>仅true时有限重试，不能无限循环</td></tr>
                <tr><td><code>error.details</code></td><td>字段、原因等上下文</td><td>用于定位具体输入问题</td></tr>
              </tbody>
            </table></div>
          </div>
          <div id="error-groups" class="error-groups"></div>
        </section>

        <section id="mcp">
          <h2>MCP Server调用规则</h2>
          <div class="card">
            <ol class="lead">
              <li>从Identity或可信MCP会话取得稳定Tenant ID与User ID，不允许LLM自由填写。</li>
              <li>只调用RAG Gateway <code>:8080</code>，不要直接连接RAGFlow或数据库。</li>
              <li>每个业务请求自动添加<code>X-Tenant-ID</code>与<code>X-User-ID</code>。</li>
              <li>管理端创建TENANT或SYSTEM共享知识库；问答默认检索当前租户可访问的全部共享库。</li>
              <li>写操作生成并复用<code>Idempotency-Key</code>；上传后轮询Job直到文档<code>READY</code>。</li>
              <li>保留Gateway返回的<code>request_id</code>、引用、warning和完整错误对象。</li>
            </ol>
          </div>
        </section>
      </main>
    </div>
    <footer>由当前运行时OpenAPI契约生成 · 文档只用于受控开发与接口评审</footer>
  </div>

  <script>
    const SPEC = {spec_json};
    const ERROR_CATALOG = {errors_json};
    const OPERATION_COPY = {operation_copy_json};
    const TAG_COPY = {tag_copy_json};
    const METHODS = new Set(["get","post","put","patch","delete"]);
    const PUBLIC_PATHS = new Set(["/health/live","/health/ready","/v1/version","/v1/capabilities"]);

    // FastAPI自动生成的OpenAPI会把422声明成HTTPValidationError，但当前Gateway运行时
    // 已通过全局异常处理器把它转换为统一RAG错误对象。响应文档采用实际运行时结构，
    // 同时在页面上明确提示这个契约差异，避免调用方错误读取detail字段。
    const RUNTIME_ERROR_SCHEMA = {{
      type:"object",
      required:["request_id","error"],
      properties:{{
        request_id:{{type:"string",format:"uuid",description:"本次失败请求的追踪ID。"}},
        error:{{
          type:"object",
          required:["code","message","retryable","details"],
          properties:{{
            code:{{type:"string",description:"稳定机器错误码，程序应使用该字段判断。"}},
            message:{{type:"string",description:"面向使用者的错误说明。"}},
            retryable:{{type:"boolean",description:"是否允许调用方稍后执行有限重试。"}},
            details:{{
              type:"array",
              description:"字段名、失败原因等错误上下文。",
              items:{{type:"object",properties:{{
                reason:{{type:"string",description:"具体失败原因。"}},
                field:{{type:"string",description:"发生错误的Header、Path、Query或Body字段。"}}
              }}}}
            }}
          }}
        }}
      }}
    }};

    // OpenAPI中多数模型没有example。这里为常见业务字段准备确定性的非业务示例，
    // 让生成结果既满足Schema类型，又比全是string/0更接近调用方实际看到的响应。
    const EXAMPLE_VALUES = {{
      request_id:"7fd67d7e-4b48-4ea7-b492-e5e0f8a35a37",
      id:"e3fa6822-45e6-4618-8412-f29669fe4218",
      knowledge_base_id:"e3fa6822-45e6-4618-8412-f29669fe4218",
      document_id:"50e87768-af8b-4d43-ae70-fe9296fd1e2b",
      version_id:"af742efd-cf64-46e1-9db0-20c553a22fb1",
      job_id:"0de6edad-0a59-4ee6-985c-e6cf507f161a",
      citation_id:"a4c2309e-b33b-41a5-9fc8-3508eb7f7e97",
      chunk_id:"chunk-01-0007",
      active_version_id:"af742efd-cf64-46e1-9db0-20c553a22fb1",
      scope_type:"TENANT",
      external_id:"example-primary-kb",
      name:"示例知识库",
      description:"用于演示接口响应结构的非业务知识库",
      filename:"example-product-guide.pdf",
      document_name:"example-product-guide.pdf",
      mime_type:"application/pdf",
      content_sha256:"9d6973900e998491b03244fa441d57ffaa83643425f64afa23e9a877b55b68b7",
      parser_profile:"deepdoc-default@1",
      chunk_profile:"prose-384@1",
      embedding_profile:"embedding-openai-compatible@3",
      retrieval_profile:"hybrid-structure-aware@6",
      generation_profile:"generation-openai-compatible@1",
      query:"示例产品的保修期限是多少？",
      original_query:"示例产品的保修期限是多少？",
      effective_query:"What is the warranty period of the example product?",
      answer:"根据已上传的产品指南，示例产品的保修期为两年。[C1]",
      quote:"示例产品自购买之日起提供两年有限保修。",
      content:"示例产品自购买之日起提供两年有限保修，具体范围以保修条款为准。",
      comment:"回答准确，引用可以定位到原文。",
      corrected_answer:"示例产品提供两年有限保修。[C1]",
      feedback_type:"answer",
      response_language:"zh-CN",
      detected_language:"zh-CN",
      requested_mode:"hybrid",
      effective_mode:"hybrid",
      cache_type:"none",
      outcome:"success",
      api_version:"v1",
      build_commit:"329b2c91ffcc",
      ragflow_contract_version:"v0.26.4",
      build_time:"2026-08-19T10:30:00Z",
      created_at:"2026-08-19T10:30:00Z",
      updated_at:"2026-08-19T10:30:05Z",
      started_at:"2026-08-19T10:30:01Z",
      finished_at:"2026-08-19T10:30:05Z",
      completed_at:"2026-08-19T10:30:05Z",
      indexed_at:"2026-08-19T10:30:05Z",
      activated_at:"2026-08-19T10:30:05Z",
      retired_at:"2026-08-18T09:00:00Z",
      role:"user",
      label:"relevant",
      type:"PARSE",
      reason:"missing",
      field:"header.X-Tenant-ID",
      message:"请求参数无效",
      code:"RAG-COMMON-001"
    }};

    // OpenAPI模型当前只声明了字段类型和约束，没有填写description。下面的字段词典
    // 为每个现有请求/响应字段提供面向调用方的中文含义；用途与必要性再由字段类别、
    // 请求/响应方向和required约束共同推导。这样新增字段即使尚未进入词典，也会得到
    // 明确的兜底说明，而不会在对外文档中出现无法理解的空白或破折号。
    const FIELD_LABELS = {{
      activated_at:"版本正式启用时间", active_version_id:"当前活动版本ID", active_version_number:"当前活动版本序号",
      answer:"最终生成答案", api_version:"对外API版本", attempt:"当前执行尝试次数", auto_parse:"上传后是否自动解析",
      bbox:"内容在页面中的边界框", build_commit:"构建对应的Git提交", build_time:"服务构建时间", cache_type:"本次回答使用的缓存类型",
      candidates:"检索候选Chunk列表", chunk_count:"文档已生成的Chunk数量", chunk_id:"RAGFlow Chunk唯一标识",
      chunk_profile:"切块策略Profile", chunk_profiles:"系统支持的切块Profile列表", citation_id:"引用记录唯一标识",
      citations:"最终答案采用的引用列表", code:"稳定业务错误码", comment:"调用方补充的反馈说明", completed_at:"任务完成时间",
      completion_tokens:"模型输出Token数量", conditions:"元数据过滤条件列表", content:"检索或引用的原始文本内容",
      content_sha256:"上传文件内容SHA-256摘要", context_top_n:"最终送入生成模型的候选数量", corrected_answer:"调用方提供的修正答案",
      created_at:"资源创建时间", ctx:"参数校验上下文", data:"成功响应的业务数据", dependencies:"外部依赖健康状态",
      description:"资源用途描述", detail:"框架参数校验错误列表", details:"业务错误的结构化细节", detected_language:"系统识别到的问题语言",
      document_count:"知识库中的文档数量", document_id:"文档唯一标识", document_ids:"限定检索范围的文档ID列表",
      document_name:"引用来源文档名称", document_revision:"文档参与检索时的修订号", effective_mode:"实际执行的检索模式",
      effective_query:"最终用于检索的问题", embedding_profile:"Embedding模型与维度Profile", error:"任务或接口错误对象",
      external_id:"调用方提供的外部系统标识", features:"当前启用的能力开关", feedback_type:"反馈分类", field:"发生错误的字段路径",
      file:"需要上传的本地文件", file_extensions:"系统允许上传的文件扩展名", file_size_bytes:"文件字节大小", filename:"上传文件名称",
      finished_at:"任务结束时间", generation_profile:"答案生成模型Profile", heading_path:"Chunk所在的章节标题路径", id:"当前资源唯一标识",
      include_content:"是否在检索结果中返回正文", include_positions:"是否返回页码和边界框", indexed_at:"文档完成索引的时间",
      input:"参数校验收到的原始输入", job_id:"异步任务唯一标识", knowledge_base_id:"知识库唯一标识",
      knowledge_base_ids:"本次检索或问答使用的知识库ID列表", knowledge_languages:"知识库内容语言集合", label:"反馈标签",
      last_error:"文档最近一次处理错误", latency_ms:"本次操作耗时毫秒数", loc:"参数校验失败位置", logic:"多个过滤条件的组合逻辑",
      max_attempts:"任务允许的最大尝试次数", max_file_bytes:"单文件允许的最大字节数", message:"错误的人类可读说明",
      messages:"可选的历史对话消息", meta:"分页或运行元信息", metadata:"文档业务元数据", metadata_filter:"检索元数据过滤表达式",
      mime_type:"文件MIME类型", mode:"调用方请求的处理模式", msg:"框架参数校验消息", name:"资源显示名称",
      operator:"元数据条件比较运算符", options:"检索结果返回选项", original_query:"调用方提交的原始问题", outcome:"问答最终结果状态",
      page:"当前分页页码", page_end:"引用内容结束页码", page_size:"每页或每次返回数量", page_start:"引用内容起始页码",
      parser_profile:"文件解析策略Profile", parser_profiles:"系统支持的解析Profile列表", positions:"引用在原文件中的位置信息",
      progress:"任务完成进度", prompt_tokens:"模型输入Token数量", query:"需要检索或回答的问题", query_plan:"系统生成的查询执行计划",
      query_rewritten:"问题是否经过改写或翻译", query_type:"系统识别的问题类型", quote:"答案实际引用的原文片段",
      ragflow_contract_version:"适配的RAGFlow契约版本", rating:"调用方评分", reason:"错误细节原因", reindex_required:"是否需要重新建立索引",
      request_id:"请求链路追踪ID", requested_mode:"调用方指定的检索模式", rerank_score:"Cross-Encoder重排相关度",
      rerank_top_n:"进入重排阶段的候选数量", response_language:"答案输出语言", retired_at:"旧版本退役时间",
      retrieval_profile:"检索编排Profile", retrieval_profiles:"系统支持的检索Profile列表", retrieval_routes:"本次执行的检索路线列表",
      retrieval_top_k:"每条基础检索路线的候选上限", retryable:"该错误是否建议自动重试", revision:"知识库内容修订号",
      role:"对话消息角色", score:"候选最终排序分数", scores:"候选各阶段分数组合", started_at:"任务开始时间",
      scope_type:"知识库共享范围类型", status:"资源或任务当前状态", term_score:"BM25关键词相关度", token_count:"文档解析后的Token数量", total:"符合条件的结果总数",
      total_tokens:"模型调用总Token数量", type:"资源、任务或校验错误类型", updated_at:"资源最后更新时间",
      usage:"生成模型Token用量", value:"元数据过滤目标值", variants:"不同语言的查询变体", vector_score:"向量语义相似度",
      version_id:"文档版本唯一标识", version_number:"文档版本序号", warnings:"不阻断请求的降级或限制说明"
    }};

    const PARAMETER_GUIDANCE = {{
      "header.Idempotency-Key":["写操作幂等键","识别同一次业务操作的网络重试，防止重复创建知识库、文档或任务。","写接口发生超时重试时客户端无法仅凭HTTP结果判断是否已经成功，因此必须使用稳定且唯一的操作键。"],
      "header.X-Tenant-ID":["Identity系统提供的稳定租户ID","确定TENANT知识库的共享范围；SYSTEM知识库对所有租户可见。","当前Direct模式没有内置身份验证，Gateway必须依靠它确定本次请求属于哪个租户。"],
      "header.X-User-ID":["Identity系统提供的稳定用户ID","标识本次调用主体并参与审计、缓存和幂等隔离，不创建个人知识库。","当前Direct模式不验证调用者是否拥有该用户身份，生产环境必须由可信Identity提供。"],
      "query.page":["分页页码","选择分页结果中的第几页，从1开始。","结果较多时避免一次返回全部数据；不传时使用第1页。"],
      "query.page_size":["每页返回数量","控制单次分页响应的数据量。","用于平衡网络响应大小与翻页次数；不传时使用接口默认值。"],
      "query.name":["知识库名称筛选词","只返回名称符合条件的知识库。","用于缩小列表结果；不传时不按名称过滤。"],
      "query.status":["资源状态筛选值","只返回指定处理状态的知识库或文档。","用于定位READY、FAILED等资源；不传时返回全部状态。"],
      "query.force":["强制删除开关","允许删除仍含有关联文档的知识库。","默认拒绝高影响级联删除，只有调用方明确确认时才执行。"],
      "query.filename":["文件名筛选词","在文档列表中按文件名缩小结果。","便于查找目标文件；不传时不按文件名过滤。"],
      "query.external_id":["外部ID筛选值","使用调用方自己的业务标识查找资源。","外部系统可能没有保存RAG UUID，因此允许用其稳定业务ID反查。"],
      "query.cancel_active":["删除前是否取消活动任务","删除文档时先请求取消仍在运行的解析或更新任务。","避免活动任务继续写入正在删除的资源；不传时采用安全默认策略。"],
      "path.knowledge_base_id":["目标知识库UUID","确定本次读取、修改、上传或删除操作作用于哪个知识库。","知识库是文档和检索权限边界，缺少它无法定位目标资源。"],
      "path.document_id":["目标文档UUID","确定本次详情、删除、重解析或版本操作作用于哪个文档。","一个共享知识库可能有多个文档，必须精确定位目标。"],
      "path.version_id":["目标文档版本UUID","确定要查询、发布或回滚的具体物理版本。","逻辑文档可以保留多个版本，必须区分目标版本。"],
      "path.job_id":["目标异步任务UUID","查询或取消特定上传、解析、重解析、删除和版本任务。","异步接口会立即返回，后续状态只能通过任务ID关联。"],
      "path.request_id":["目标问答请求UUID","查询某次问答产生的全部引用记录。","引用与一次具体回答绑定，需要请求ID建立关联。"],
      "path.citation_id":["目标引用UUID","读取一条引用的完整原文、位置和评分。","回答可能包含多条引用，需要唯一ID定位用户点击的来源。"]
    }};

    const ID_FIELDS = new Set(["id","request_id","knowledge_base_id","document_id","version_id","job_id","citation_id","chunk_id","active_version_id"]);
    const TIME_FIELDS = new Set(["created_at","updated_at","started_at","finished_at","completed_at","indexed_at","activated_at","retired_at","build_time"]);
    const PROFILE_FIELDS = new Set(["parser_profile","chunk_profile","embedding_profile","retrieval_profile","generation_profile","parser_profiles","chunk_profiles","retrieval_profiles"]);
    const SCORE_FIELDS = new Set(["score","scores","vector_score","term_score","rerank_score"]);
    const ERROR_FIELDS = new Set(["error","last_error","code","message","retryable","details","detail","reason","field","loc","msg","ctx","input"]);

    function fieldGuidance(path, schema, originalDescription, required, direction) {{
      const fieldName = path.replace(/\\[\\]/g, "").split(".").pop();
      const meaning = originalDescription && originalDescription !== "—"
        ? originalDescription
        : (FIELD_LABELS[fieldName] || `字段 ${{fieldName}}`);
      let use = "承载该接口处理或返回所需的业务信息。";
      if (ID_FIELDS.has(fieldName) || fieldName.endsWith("_ids")) use = "唯一定位并关联对应资源，同时执行TENANT/SYSTEM共享范围校验。";
      else if (TIME_FIELDS.has(fieldName)) use = "用于审计、排序、耗时分析和判断资源生命周期。";
      else if (PROFILE_FIELDS.has(fieldName)) use = "固定本次处理采用的算法、模型和参数版本，保证行为可复现。";
      else if (SCORE_FIELDS.has(fieldName)) use = "解释候选的相关性来源，并支持调用方展示或质量分析。";
      else if (ERROR_FIELDS.has(fieldName)) use = "帮助调用方定位失败位置、决定是否重试并记录可追踪错误。";
      else if (fieldName.includes("query") || fieldName === "variants" || fieldName === "retrieval_routes") use = "描述用户检索意图以及Gateway实际采用的查询规划。";
      else if (fieldName.includes("count") || ["total","page","page_size","retrieval_top_k","rerank_top_n","context_top_n"].includes(fieldName)) use = "控制或说明结果规模，避免返回量不可控。";
      else if (fieldName.includes("content") || ["answer","quote","corrected_answer","candidates","citations"].includes(fieldName)) use = "向调用方提供检索、生成或引用所需的实际业务内容。";
      else if (fieldName.includes("metadata") || ["conditions","logic","operator","value"].includes(fieldName)) use = "限定检索边界或携带结构化业务属性，避免跨范围召回。";
      else if (fieldName === "status" || fieldName === "outcome" || fieldName === "progress") use = "让调用方判断资源是否可用、任务是否结束以及下一步该做什么。";
      else if (schemaType(schema) === "boolean") use = "显式开启或关闭对应行为，避免服务端猜测调用意图。";

      let why;
      if (direction === "request") {{
        why = required
          ? "这是请求契约必填字段；缺失时服务无法完整定位目标或执行操作，将拒绝请求。"
          : "这是可选控制字段；不传时服务使用默认值或不启用该筛选/覆盖行为。";
      }} else {{
        why = required
          ? "这是成功响应契约中的稳定字段，调用方可以直接读取并据此继续后续流程。"
          : "仅在对应场景有数据时返回，用于补充诊断、展示或高级处理；调用方应允许为空。";
      }}
      return {{meaning,use,why}};
    }}

    const esc = (value) => String(value ?? "").replace(/[&<>\"']/g, (char) => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;","'":"&#39;"}}[char]));

    function resolveRef(node) {{
      if (!node || typeof node !== "object" || !node.$ref) return node || {{}};
      return node.$ref.replace(/^#\\//, "").split("/").reduce((value, key) => value?.[key], SPEC) || {{}};
    }}

    function schemaType(schema) {{
      const resolved = resolveRef(schema);
      if (Array.isArray(resolved.type)) return resolved.type.join(" | ");
      if (resolved.type === "array") return `array<${{schemaType(resolved.items)}}>`;
      return resolved.type || (resolved.properties ? "object" : resolved.oneOf ? "oneOf" : "—");
    }}

    function constraints(schema) {{
      const s = resolveRef(schema); const items = [];
      if (s.format) items.push(`format=${{s.format}}`);
      if (s.default !== undefined) items.push(`默认=${{JSON.stringify(s.default)}}`);
      if (s.enum) items.push(`可选=${{s.enum.map(String).join(" / ")}}`);
      if (s.minimum !== undefined) items.push(`最小=${{s.minimum}}`);
      if (s.maximum !== undefined) items.push(`最大=${{s.maximum}}`);
      if (s.minLength !== undefined) items.push(`最短=${{s.minLength}}`);
      if (s.maxLength !== undefined) items.push(`最长=${{s.maxLength}}`);
      if (s.minItems !== undefined) items.push(`最少${{s.minItems}}项`);
      if (s.maxItems !== undefined) items.push(`最多${{s.maxItems}}项`);
      if (s.pattern) items.push(`格式=${{s.pattern}}`);
      return items.join("；") || "—";
    }}

    function preferredVariant(schema) {{
      const variants = schema?.oneOf || schema?.anyOf || [];
      return variants.find((item) => resolveRef(item).type !== "null") || variants[0] || schema;
    }}

    // 同一个字段名 `id` 会出现在知识库、文档、版本、任务等多种模型中。
    // 这里根据当前Schema名称挑选对应的示例ID，避免文档接口却展示知识库ID。
    function contextualExampleValue(fieldName, schemaName="") {{
      if (fieldName !== "id") return EXAMPLE_VALUES[fieldName];
      if (/DocumentVersion/i.test(schemaName)) return EXAMPLE_VALUES.version_id;
      if (/Document/i.test(schemaName)) return EXAMPLE_VALUES.document_id;
      if (/KnowledgeBase/i.test(schemaName)) return EXAMPLE_VALUES.knowledge_base_id;
      if (/Job/i.test(schemaName)) return EXAMPLE_VALUES.job_id;
      if (/Citation/i.test(schemaName)) return EXAMPLE_VALUES.citation_id;
      return EXAMPLE_VALUES.id;
    }}

    function exampleForSchema(schema, depth=0, seen=new Set(), fieldName="", schemaName="") {{
      if (depth > 7) return "…";
      let currentSchemaName = schemaName;
      if (schema?.$ref) {{
        if (seen.has(schema.$ref)) return "…";
        seen = new Set(seen); seen.add(schema.$ref);
        currentSchemaName = schema.$ref.split("/").pop() || schemaName;
      }}
      let s = resolveRef(schema);
      if (s.oneOf?.length || s.anyOf?.length) s = resolveRef(preferredVariant(s));
      if (s.example !== undefined) return s.example;
      if (Array.isArray(s.examples) && s.examples.length) return s.examples[0];
      if (s.default !== undefined) return s.default;

      if (s.enum?.length) {{
        if (fieldName === "status") return ["READY","SUCCEEDED","RUNNING","success"].find((value) => s.enum.includes(value)) || s.enum[0];
        const contextualValue = contextualExampleValue(fieldName, currentSchemaName);
        if (contextualValue !== undefined && s.enum.includes(contextualValue)) return contextualValue;
        return s.enum[0];
      }}
      const contextualValue = contextualExampleValue(fieldName, currentSchemaName);
      if (contextualValue !== undefined && (s.type === "string" || !s.type)) return contextualValue;

      const numberExamples = {{
        revision:3, document_revision:3, version_number:2, active_version_number:2,
        page:1, page_size:20, total:1, chunk_count:7, token_count:1099,
        file_size_bytes:40937, progress:1, attempt:1, max_attempts:2,
        score:0.8364, vector_score:0.8127, term_score:0.2741, rerank_score:0.9042,
        latency_ms:1284, retrieval_top_k:30, rerank_top_n:8, context_top_n:5,
        prompt_tokens:1832, completion_tokens:126, total_tokens:1958,
        page_start:1, page_end:1
      }};
      if ((s.type === "integer" || s.type === "number") && numberExamples[fieldName] !== undefined) return numberExamples[fieldName];

      if (s.type === "array") {{
        const namedArrays = {{
          knowledge_base_ids:[EXAMPLE_VALUES.knowledge_base_id],
          document_ids:[EXAMPLE_VALUES.document_id],
          retrieval_routes:["vector:original:zh-CN","term:zh-CN","vector:translated:en-US","term:en-US"],
          knowledge_languages:["zh-CN","en-US"],
          warnings:["rerank_disabled_vector_bm25_rrf_fallback"],
          heading_path:["第一章 产品说明","1.2 保修政策"],
          bbox:[85.4,110.2,510.3,155.8],
          loc:["header","X-Tenant-ID"]
        }};
        return namedArrays[fieldName] || [exampleForSchema(s.items || {{}}, depth+1, seen, fieldName.replace(/s$/, ""), currentSchemaName)];
      }}
      if (s.type === "object" || s.properties) {{
        if (!s.properties || !Object.keys(s.properties).length) {{
          const namedObjects = {{
            metadata:{{source:"api-doc-example",language:"zh-CN",document_type:"product-guide"}},
            variants:{{"zh-CN":"示例产品的保修期限是多少？","en-US":"What is the warranty period of the example product?"}},
            scores:{{vector:0.8127,term:0.2741,rerank:0.9042}},
            dependencies:{{postgres:"healthy",redis:"healthy",ragflow:"healthy"}},
            features:{{rerank:true,semantic_cache:true,document_versions:true}},
            options:{{include_content:true,include_positions:true}}
          }};
          return namedObjects[fieldName] || {{}};
        }}
        const value = {{}};
        for (const [key, child] of Object.entries(s.properties || {{}})) value[key] = exampleForSchema(child, depth+1, seen, key, currentSchemaName);
        return value;
      }}
      if (s.format === "uuid") return contextualValue || "00000000-0000-4000-8000-000000000000";
      if (s.format === "date-time") return EXAMPLE_VALUES[fieldName] || "2026-08-19T10:30:00Z";
      if (s.format === "binary") return "<本地文件>";
      if (s.type === "integer" || s.type === "number") return s.minimum ?? 0;
      if (s.type === "boolean") return ["include_content","include_positions","auto_parse"].includes(fieldName);
      return contextualValue || "示例值";
    }}

    function flattenedSchemaRows(schema, prefix="", depth=0, seen=new Set()) {{
      if (depth > 7) return [];
      if (schema?.$ref) {{
        if (seen.has(schema.$ref)) return [];
        seen = new Set(seen); seen.add(schema.$ref);
      }}
      let s = resolveRef(schema);
      if (s.oneOf?.length || s.anyOf?.length) s = resolveRef(preferredVariant(s));
      if (s.type === "array") return flattenedSchemaRows(s.items || {{}}, `${{prefix}}[]`, depth+1, seen);
      if (!s.properties) return [];

      const required = new Set(s.required || []); const rows = [];
      for (const [name, child] of Object.entries(s.properties)) {{
        const path = prefix ? `${{prefix}}.${{name}}` : name;
        let resolved = resolveRef(child);
        if (resolved.oneOf?.length || resolved.anyOf?.length) resolved = resolveRef(preferredVariant(resolved));
        rows.push({{path,required:required.has(name),schema:child,description:resolved.description || "—"}});
        if (resolved.type === "array") rows.push(...flattenedSchemaRows(resolved.items || {{}}, `${{path}}[]`, depth+1, seen));
        else if (resolved.type === "object" || resolved.properties) rows.push(...flattenedSchemaRows(child, path, depth+1, seen));
      }}
      return rows;
    }}

    function schemaRows(schema, recursive=false, direction="response") {{
      const rows = recursive ? flattenedSchemaRows(schema) : (() => {{
        let s = resolveRef(schema);
        if (s.oneOf?.length || s.anyOf?.length) s = resolveRef(preferredVariant(s));
        const required = new Set(s.required || []);
        return Object.entries(s.properties || {{}}).map(([name, child]) => ({{path:name,required:required.has(name),schema:child,description:resolveRef(child).description || "—"}}));
      }})();
      return rows.map((row) => {{
        const guidance = fieldGuidance(row.path,row.schema,row.description,row.required,direction);
        return `<tr><td><code>${{esc(row.path)}}</code></td><td>${{row.required ? "是" : "否"}}</td><td>${{esc(schemaType(row.schema))}}</td><td>${{esc(constraints(row.schema))}}</td><td>${{esc(guidance.meaning)}}</td><td>${{esc(guidance.use)}}</td><td>${{esc(guidance.why)}}</td></tr>`;
      }}).join("");
    }}

    function schemaTable(schema, recursive=false, direction="response") {{
      const rows = schemaRows(schema, recursive, direction);
      if (!rows) return `<p class="lead">模型类型：<code>${{esc(schemaType(schema))}}</code></p>`;
      return `<div class="table-wrap"><table><thead><tr><th>字段路径</th><th>必填</th><th>类型</th><th>约束</th><th>参数是什么意思</th><th>有什么用</th><th>为什么需要</th></tr></thead><tbody>${{rows}}</tbody></table></div>`;
    }}

    function derefParameter(parameter) {{ return resolveRef(parameter); }}

    function collectOperations() {{
      const operations = [];
      for (const [path, pathItem] of Object.entries(SPEC.paths || {{}})) {{
        for (const [method, operation] of Object.entries(pathItem)) {{
          if (!METHODS.has(method.toLowerCase())) continue;
          operations.push({{path, method:method.toUpperCase(), operation, commonParameters:pathItem.parameters || []}});
        }}
      }}
      return operations;
    }}

    function parameterTable(parameters) {{
      if (!parameters.length) return "";
      const rows = parameters.map((raw) => {{
        const p = derefParameter(raw); const schema = resolveRef(p.schema || {{}});
        const special = PARAMETER_GUIDANCE[`${{p.in}}.${{p.name}}`];
        const guidance = special
          ? {{meaning:special[0],use:special[1],why:special[2]}}
          : fieldGuidance(p.name,p.schema || {{}},p.description || schema.description || "—",Boolean(p.required),"request");
        return `<tr><td><code>${{esc(p.name)}}</code></td><td>${{esc(p.in)}}</td><td>${{p.required ? "是" : "否"}}</td><td>${{esc(schemaType(p.schema || {{}}))}}</td><td>${{esc(constraints(p.schema || {{}}))}}</td><td>${{esc(guidance.meaning)}}</td><td>${{esc(guidance.use)}}</td><td>${{esc(guidance.why)}}</td></tr>`;
      }}).join("");
      return `<div class="subsection"><h4>Header / Path / Query参数</h4><div class="table-wrap"><table><thead><tr><th>参数</th><th>位置</th><th>必填</th><th>类型</th><th>约束</th><th>参数是什么意思</th><th>有什么用</th><th>为什么需要</th></tr></thead><tbody>${{rows}}</tbody></table></div></div>`;
    }}

    function requestBodyBlock(operation) {{
      if (!operation.requestBody) return "";
      const body = resolveRef(operation.requestBody); const entries = Object.entries(body.content || {{}});
      return entries.map(([contentType, content]) => {{
        const schema = content.schema || {{}}; const example = exampleForSchema(schema);
        return `<div class="subsection"><h4>Request Body · <code>${{esc(contentType)}}</code>${{body.required ? " · 必填" : " · 可选"}}</h4>${{schemaTable(schema,true,"request")}}<div class="subsection"><h4>输入格式示例</h4><div class="code-shell"><button class="copy-button" type="button">复制</button><pre>${{esc(JSON.stringify(example,null,2))}}</pre></div></div></div>`;
      }}).join("");
    }}

    function runtimeErrorExample(status) {{
      const catalog = ERROR_CATALOG.find((item) => String(item.http).includes(String(status)));
      const validation = String(status) === "422";
      return {{
        request_id:EXAMPLE_VALUES.request_id,
        error:{{
          code:validation ? "RAG-COMMON-001" : (catalog?.code || "RAG-COMMON-011"),
          message:validation ? "请求参数无效" : (catalog?.message || "服务内部错误"),
          retryable:validation ? false : String(catalog?.retryable || "").toLowerCase().includes("true"),
          details:validation ? [{{reason:"missing",field:"header.X-Tenant-ID"}}] : []
        }}
      }};
    }}

    function responseBlock(operation) {{
      const entries = Object.entries(operation.responses || {{}});
      if (!entries.length) return "";
      const cards = entries.map(([status, raw]) => {{
        const response = resolveRef(raw); const isError = Number(status) >= 400;
        const [contentType, content] = Object.entries(response.content || {{}})[0] || [];
        const declaredSchema = content?.schema || {{}};
        const effectiveSchema = isError ? RUNTIME_ERROR_SCHEMA : declaredSchema;
        const example = isError ? runtimeErrorExample(status) : exampleForSchema(effectiveSchema);
        const fieldCount = flattenedSchemaRows(effectiveSchema).length;
        const description = response.description || (isError ? "请求失败" : "请求成功");
        const contractNote = isError && declaredSchema?.$ref?.endsWith("/HTTPValidationError")
          ? `<div class="runtime-note"><b>运行时实际格式：</b>OpenAPI自动声明为HTTPValidationError，但Gateway全局异常处理器会把参数错误转换为下方统一RAG错误对象。</div>`
          : "";
        const fieldTable = content
          ? schemaTable(effectiveSchema, true, "response")
          : `<p class="lead">该状态没有响应体。</p>`;
        const exampleText = content ? JSON.stringify(example,null,2) : "<无响应体>";
        return `<details class="response ${{isError ? "error" : ""}}">
          <summary>
            <span class="status">HTTP ${{esc(status)}}</span>
            <span class="response-summary-copy"><strong>${{esc(description)}}</strong><p>${{esc(contentType || "无响应体")}} · 点击查看字段和完整示例</p></span>
            <span class="response-chevron">⌄</span>
          </summary>
          <div class="response-body">
            ${{contractNote}}
            <details class="response-part">
              <summary><span>完整字段结构</span><span><span class="field-count">${{fieldCount}}个字段</span> <span class="response-chevron">⌄</span></span></summary>
              <div class="response-part-content">${{fieldTable}}</div>
            </details>
            <details class="response-part">
              <summary><span>JSON 响应示例</span><span><span class="field-count">非业务示例</span> <span class="response-chevron">⌄</span></span></summary>
              <div class="response-part-content"><div class="code-shell"><button class="copy-button" type="button">复制</button><pre>${{esc(exampleText)}}</pre></div></div>
            </details>
          </div>
        </details>`;
      }}).join("");
      return `<div class="subsection"><h4>Response · 点击状态码展开</h4><div class="response-grid">${{cards}}</div></div>`;
    }}

    function curlExample(item, parameters) {{
      const {{path,method,operation}} = item; const isPublic = PUBLIC_PATHS.has(path);
      let url = `${{{api_base_url_js}}}${{path.replace(/\\{{([^}}]+)\\}}/g,"<$1>")}}`;
      const requiredQuery = parameters.map(derefParameter).filter((p) => p.in === "query" && p.required);
      if (requiredQuery.length) url += "?" + requiredQuery.map((p) => `${{p.name}}=<${{p.name}}>`).join("&");
      const lines = [`curl -X ${{method}} \\\n  '${{url}}'`];
      if (!isPublic) {{
        lines.push(`  -H 'X-Tenant-ID: identity-tenant-123'`);
        lines.push(`  -H 'X-User-ID: identity-user-123'`);
      }}
      const hasIdempotency = parameters.map(derefParameter).some((p) => p.name === "Idempotency-Key");
      if (hasIdempotency) lines.push(`  -H 'Idempotency-Key: unique-operation-key-001'`);
      if (operation.requestBody) {{
        const body = resolveRef(operation.requestBody); const [contentType, content] = Object.entries(body.content || {{}})[0] || [];
        if (contentType === "application/json") {{
          lines.push(`  -H 'Content-Type: application/json'`);
          lines.push(`  -d '${{JSON.stringify(exampleForSchema(content.schema || {{}}),null,2)}}'`);
        }} else if (contentType === "multipart/form-data") {{
          lines.push(`  -F 'file=@/path/to/file.pdf'`);
        }}
      }}
      return lines.join(" \\\n");
    }}

    function renderEndpoint(item) {{
      const {{path,method,operation,commonParameters}} = item;
      const operationId = operation.operationId || `${{method}} ${{path}}`;
      const [title,description] = OPERATION_COPY[operationId] || [operation.summary || operationId, operation.description || "请参考当前OpenAPI契约。"];
      const parameters = [...commonParameters,...(operation.parameters || [])];
      const isPublic = PUBLIC_PATHS.has(path);
      const hasIdempotency = parameters.map(derefParameter).some((p) => p.name === "Idempotency-Key");
      const search = [path,method,operationId,title,description,...(operation.tags || [])].join(" ").toLowerCase();
      return `<details class="endpoint" data-method="${{method}}" data-search="${{esc(search)}}">
        <summary><span class="method ${{method}}">${{method}}</span><span class="path">${{esc(path)}}</span><span class="summary-title">${{esc(title)}}</span></summary>
        <div class="endpoint-body">
          <p class="endpoint-desc">${{esc(description)}}</p>
          <div class="chips"><span class="chip">${{esc(operationId)}}</span><span class="chip ${{isPublic ? "" : "required"}}">${{isPublic ? "公开接口" : "需要 Tenant/User"}}</span>${{hasIdempotency ? '<span class="chip required">需要 Idempotency-Key</span>' : ''}}</div>
          ${{parameterTable(parameters)}}
          ${{requestBodyBlock(operation)}}
          <div class="subsection"><h4>调用示例</h4><div class="code-shell"><button class="copy-button" type="button">复制</button><pre>${{esc(curlExample(item,parameters))}}</pre></div></div>
          ${{responseBlock(operation)}}
        </div>
      </details>`;
    }}

    function render() {{
      const operations = collectOperations();
      const grouped = new Map();
      for (const item of operations) {{ const tag = item.operation.tags?.[0] || "Other"; if (!grouped.has(tag)) grouped.set(tag,[]); grouped.get(tag).push(item); }}
      document.querySelector("#stat-operations").textContent = operations.length;
      document.querySelector("#hero-operation-count").textContent = `${{operations.length}}个接口`;
      document.querySelector("#stat-tags").textContent = grouped.size;
      document.querySelector("#stat-schemas").textContent = Object.keys(SPEC.components?.schemas || {{}}).length;
      document.querySelector("#stat-errors").textContent = ERROR_CATALOG.length;
      document.querySelector("#side-nav .count").textContent = operations.length;

      const moduleGrid = document.querySelector("#module-grid");
      const endpointGroups = document.querySelector("#endpoint-groups");
      for (const [tag,items] of grouped.entries()) {{
        const [name,desc] = TAG_COPY[tag] || [tag,"接口模块"];
        moduleGrid.insertAdjacentHTML("beforeend",`<div class="module-card"><b>${{esc(name)}} · ${{items.length}}个接口</b><p>${{esc(desc)}}</p></div>`);
        endpointGroups.insertAdjacentHTML("beforeend",`<div class="tag-group" data-tag="${{esc(tag)}}"><div class="tag-head"><div><h3>${{esc(name)}}</h3><p>${{esc(desc)}}</p></div><span class="tag-count">${{items.length}}个接口</span></div>${{items.map(renderEndpoint).join("")}}</div>`);
        document.querySelector("#side-nav").insertAdjacentHTML("beforeend",`<a href="#tag-${{esc(tag)}}" onclick="document.querySelector('[data-tag=&quot;${{esc(tag)}}&quot;]').scrollIntoView()"><span>${{esc(name)}}</span><span class="count">${{items.length}}</span></a>`);
      }}

      const prefixNames = {{COMMON:"通用与请求",KB:"知识库",DOC:"文档与版本",JOB:"异步任务",RETRIEVAL:"检索",CONTEXT:"上下文",LLM:"模型生成",CITATION:"引用",RF:"RAGFlow",DB:"数据库",CACHE:"缓存",CONFIG:"配置与模型"}};
      const byPrefix = new Map();
      for (const error of ERROR_CATALOG) {{ const prefix = error.code.split("-")[1]; if (!byPrefix.has(prefix)) byPrefix.set(prefix,[]); byPrefix.get(prefix).push(error); }}
      for (const [prefix,entries] of byPrefix.entries()) {{
        const rows = entries.map((e) => `<tr><td><code>${{esc(e.code)}}</code></td><td>${{esc(e.http)}}</td><td>${{esc(e.retryable)}}</td><td>${{esc(e.message)}}</td></tr>`).join("");
        document.querySelector("#error-groups").insertAdjacentHTML("beforeend",`<div class="error-group"><h3>${{esc(prefixNames[prefix] || prefix)}} · ${{entries.length}}项</h3><div class="table-wrap" style="border:0;border-radius:0"><table><thead><tr><th>错误码</th><th>HTTP</th><th>可重试</th><th>错误上下文</th></tr></thead><tbody>${{rows}}</tbody></table></div></div>`);
      }}
    }}

    function applyFilter() {{
      const query = document.querySelector("#search").value.trim().toLowerCase();
      const method = document.querySelector("#method-filter").value; let visible = 0;
      document.querySelectorAll("details.endpoint").forEach((element) => {{
        const match = (!query || element.dataset.search.includes(query)) && (method === "ALL" || element.dataset.method === method);
        element.hidden = !match; if (match) visible++;
      }});
      document.querySelectorAll(".tag-group").forEach((group) => {{ group.hidden = !group.querySelector("details.endpoint:not([hidden])"); }});
      document.querySelector("#empty").style.display = visible ? "none" : "block";
    }}

    render();
    document.querySelector("#search").addEventListener("input",applyFilter);
    document.querySelector("#method-filter").addEventListener("change",applyFilter);
    document.querySelector("#expand-all").addEventListener("click",(event) => {{
      const visible = [...document.querySelectorAll("details.endpoint:not([hidden])")]; const shouldOpen = visible.some((item) => !item.open);
      visible.forEach((item) => {{
        item.open = shouldOpen;
        item.querySelectorAll("details.response, details.response-part").forEach((nested) => nested.open = shouldOpen);
      }});
      event.currentTarget.textContent = shouldOpen ? "收起全部" : "展开全部";
    }});
    document.querySelector("#print-doc").addEventListener("click",() => window.print());
    async function copyText(text) {{
      if (navigator.clipboard?.writeText) {{ try {{ await navigator.clipboard.writeText(text); return true; }} catch {{ /* file://下继续使用兼容方案。 */ }} }}
      const textarea = document.createElement("textarea");
      textarea.value = text; textarea.setAttribute("readonly", ""); textarea.style.position = "fixed"; textarea.style.opacity = "0";
      document.body.appendChild(textarea); textarea.select();
      try {{ return document.execCommand("copy"); }} finally {{ textarea.remove(); }}
    }}
    document.addEventListener("click",async(event) => {{
      const button = event.target.closest(".copy-button"); if (!button) return;
      const text = button.parentElement.querySelector("pre")?.textContent || "";
      if (await copyText(text)) {{ button.textContent="已复制"; setTimeout(() => button.textContent="复制",1200); }}
      else button.textContent="复制失败";
    }});
  </script>
</body>
</html>
"""


def main() -> None:
    """执行单次构建并打印不含任何密钥的结果摘要。"""

    spec = load_json(OPENAPI_PATH)
    errors = parse_error_catalog(ERRORS_PATH)
    output = build_html(spec, errors)
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(OUTPUT_PATH),
                "bytes": OUTPUT_PATH.stat().st_size,
                "operations": sum(
                    1
                    for path_item in spec.get("paths", {}).values()
                    for method in path_item
                    if method.lower() in {"get", "post", "put", "patch", "delete"}
                ),
                "errors": len(errors),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

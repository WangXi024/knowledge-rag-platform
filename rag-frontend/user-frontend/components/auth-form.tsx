"use client";

/** 开发联调身份表单：只提交 Tenant ID 和 User ID，不包含注册、密码或身份验证。 */

import { ArrowRight, Building2, CheckCircle2, Globe2, LibraryBig, MessageSquareText, ShieldAlert, Sparkles, UserRound } from "lucide-react";
import { FormEvent, useState } from "react";

export function AuthForm() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tenantId: String(form.get("tenantId") ?? ""),
          userId: String(form.get("userId") ?? ""),
        }),
      });
      const payload = await response.json() as { error?: string };
      if (!response.ok) throw new Error(payload.error || "无法进入知识空间");
      window.location.href = "/";
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-story">
        <div className="brand-mark"><Sparkles size={19} /> LazyTT</div>
        <div className="story-copy">
          <span className="eyebrow">SHARED KNOWLEDGE SPACE</span>
          <h1>一个入口，浏览<br />企业与系统共享知识。</h1>
          <p>当前租户的全部共享知识库和全局系统知识库会自动汇总，问答时无需逐个选择。</p>
          <div className="story-points">
            <div><Building2 size={19} /><span><strong>租户共享</strong><small>同一 Tenant 下的成员查看相同企业知识</small></span></div>
            <div><Globe2 size={19} /><span><strong>系统共享</strong><small>所有租户都可以使用平台统一知识</small></span></div>
            <div><MessageSquareText size={19} /><span><strong>统一问答</strong><small>自动匹配当前身份可访问的全部知识库</small></span></div>
          </div>
        </div>
        <p className="story-foot">Tenant-shared and system-shared knowledge, clearly labeled.</p>
      </section>
      <section className="auth-panel">
        <div className="auth-card">
          <span className="auth-icon"><LibraryBig size={21} /></span>
          <h2>进入共享知识空间</h2>
          <p>填写上层 Identity 系统传递的两个标识。当前阶段不需要邮箱、密码或注册账号。</p>
          <div className="identity-warning"><ShieldAlert size={17} /><span><strong>开发联调身份</strong>当前不会验证你是否拥有所填身份，请勿作为生产登录使用。</span></div>
          <form onSubmit={submit}>
            <label className="form-field">
              <span>Tenant ID</span>
              <span className="identity-input"><Building2 size={17} /><input name="tenantId" type="text" autoComplete="organization" maxLength={128} required placeholder="例如 tenant-a" /></span>
            </label>
            <label className="form-field">
              <span>User ID</span>
              <span className="identity-input"><UserRound size={17} /><input name="userId" type="text" autoComplete="username" maxLength={128} required placeholder="例如 user-001" /></span>
            </label>
            {error ? <div className="form-error">{error}</div> : null}
            <button className="primary-action auth-submit" disabled={busy}>
              {busy ? <span className="loading-dot" /> : <CheckCircle2 size={17} />}
              {busy ? "正在进入…" : "进入知识空间"}
              {!busy ? <ArrowRight size={17} /> : null}
            </button>
          </form>
        </div>
      </section>
    </main>
  );
}

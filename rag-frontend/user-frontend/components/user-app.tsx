"use client";

/** 用户应用外壳：加载 Tenant/User 联调身份，在共享知识库浏览与统一问答之间切换。 */

import { LibraryBig, LogOut, Menu, MessageSquareText, Sparkles, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { PublicUser } from "@/lib/ui-types";
import { ChatView } from "./chat-view";
import { KnowledgeView } from "./knowledge-view";

type ProductView = "knowledge" | "chat";

export function UserApp() {
  const [user, setUser] = useState<PublicUser>();
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<ProductView>("knowledge");
  const [mobileMenu, setMobileMenu] = useState(false);

  useEffect(() => {
    void fetch("/api/auth/me", { cache: "no-store" }).then(async (response) => {
      if (!response.ok) { window.location.href = "/login"; return; }
      const payload = await response.json() as { user: PublicUser };
      setUser(payload.user);
      setLoading(false);
    }).catch(() => { window.location.href = "/login"; });
  }, []);

  function selectView(nextView: ProductView) {
    /** 导航只改变当前只读产品页，不会创建知识库、上传文档或更换身份。 */
    setView(nextView);
    setMobileMenu(false);
  }

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.href = "/login";
  }

  if (loading) return <div className="app-loading"><span className="assistant-mark"><Sparkles size={20} /></span><div><strong>正在进入共享知识空间</strong><small>正在加载 Tenant/User 身份</small></div><span className="loading-line" /></div>;

  const avatar = user?.userId.slice(0, 1).toUpperCase() || "U";

  return (
    <main className="product-shell">
      <header className="mobile-header"><button onClick={() => setMobileMenu(true)}><Menu size={20} /></button><span className="brand-mark"><Sparkles size={17} /> LazyTT</span><span className="mobile-avatar">{avatar}</span></header>
      <aside className={`product-sidebar ${mobileMenu ? "mobile-open" : ""}`}>
        <div className="sidebar-brand"><span><Sparkles size={19} /></span><div><strong>LazyTT</strong><small>Knowledge Assistant</small></div><button className="mobile-close" onClick={() => setMobileMenu(false)}><X size={18} /></button></div>
        <div className="space-card"><LibraryBig size={18} /><div><strong>共享知识空间</strong><small>租户知识 + 系统知识</small></div><span /></div>
        <nav className="main-nav">
          <button className={view === "knowledge" ? "active" : ""} onClick={() => selectView("knowledge")}><LibraryBig size={19} /><span>共享知识库</span></button>
          <button className={view === "chat" ? "active" : ""} onClick={() => selectView("chat")}><MessageSquareText size={19} /><span>智能问答</span></button>
        </nav>
        <div className="sidebar-tip"><Sparkles size={15} /><p>问答会自动检索当前租户共享知识库和全部系统共享知识库。</p></div>
        <footer className="sidebar-user">
          <span className="user-avatar">{avatar}</span>
          <div><strong title={`User ID: ${user?.userId}`}>{user?.userId}</strong><small title={`Tenant ID: ${user?.tenantId}`}>Tenant: {user?.tenantId}</small></div>
          <button onClick={() => void logout()} title="切换身份"><LogOut size={17} /></button>
        </footer>
      </aside>
      <section className="product-main">
        {view === "knowledge" ? <KnowledgeView tenantId={user?.tenantId || ""} /> : <ChatView />}
      </section>
      {mobileMenu ? <button className="mobile-backdrop" aria-label="关闭菜单" onClick={() => setMobileMenu(false)} /> : null}
    </main>
  );
}

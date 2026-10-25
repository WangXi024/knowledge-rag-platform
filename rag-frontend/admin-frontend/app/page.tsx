'use client';

/**
 * 管理端页面入口：在浏览器本地保存开发身份，展示身份选择与后台壳层。
 * 本文件不直接访问 Gateway；知识库和文档请求全部交给 AdminWorkspace 的同源 BFF 调用。
 */

import {
  BookOpen,
  Building2,
  ChevronRight,
  Database,
  FileStack,
  Globe2,
  LayoutDashboard,
  LogOut,
  ShieldAlert,
} from 'lucide-react';
import { FormEvent, useEffect, useState } from 'react';
import { AdminWorkspace, type AdminSection } from '@/components/admin-workspace';
import type { AdminRole, AdminSession } from '@/lib/contracts';

const SESSION_KEY = 'lazytt-admin-session';

/**
 * 管理端入口：恢复本机开发身份，或展示角色选择表单；业务资源由工作区组件负责。
 */
export default function Home() {
  const [session, setSession] = useState<AdminSession | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    // 延迟到浏览器任务队列再同步 localStorage，避免 SSR 水合内容不一致。
    const timeout = window.setTimeout(() => {
      try {
        const value = window.localStorage.getItem(SESSION_KEY);
        if (value) {
          const candidate = JSON.parse(value) as Partial<AdminSession>;
          const validRole = candidate.role === 'TENANT_ADMIN' || candidate.role === 'SYSTEM_ADMIN';
          if (validRole && typeof candidate.tenantId === 'string' && typeof candidate.userId === 'string') {
            setSession(candidate as AdminSession);
          } else {
            window.localStorage.removeItem(SESSION_KEY);
          }
        }
      } catch {
        window.localStorage.removeItem(SESSION_KEY);
      } finally {
        setReady(true);
      }
    }, 0);
    return () => window.clearTimeout(timeout);
  }, []);

  function handleLogin(nextSession: AdminSession) {
    // 本地存储只保存非可信的 Tenant/User/Role 开发标识，不存密码、Token 或 JWT。
    window.localStorage.setItem(SESSION_KEY, JSON.stringify(nextSession));
    setSession(nextSession);
  }

  function handleLogout() {
    // 退出只清除当前浏览器身份，不调用后端，也不会删除任何知识库或业务数据。
    window.localStorage.removeItem(SESSION_KEY);
    setSession(null);
  }

  if (!ready) {
    return <main className="loading-screen">正在加载管理端…</main>;
  }

  if (!session) {
    return <LoginScreen onLogin={handleLogin} />;
  }

  return <AdminShell session={session} onLogout={handleLogout} />;
}

/**
 * 开发阶段的身份入口。系统管理员不属于某个企业租户，因此界面隐藏租户输入，
 * 请求层后续会为必填的 X-Tenant-ID 使用固定的内部管理上下文。
 */
function LoginScreen({ onLogin }: { onLogin: (session: AdminSession) => void }) {
  const [role, setRole] = useState<AdminRole>('TENANT_ADMIN');
  const [tenantId, setTenantId] = useState('tenant-a');
  const [userId, setUserId] = useState('admin-001');
  const [error, setError] = useState('');

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    // Tenant 管理员必须给出所属 Tenant；系统管理员使用固定内部上下文满足当前 Header 契约。
    event.preventDefault();
    const cleanUserId = userId.trim();
    const cleanTenantId = tenantId.trim();

    if (!cleanUserId || (role === 'TENANT_ADMIN' && !cleanTenantId)) {
      setError('请完整填写当前管理员身份信息。');
      return;
    }

    onLogin({
      role,
      tenantId: role === 'TENANT_ADMIN' ? cleanTenantId : 'platform-management',
      userId: cleanUserId,
    });
  }

  return (
    <main className="login-page">
      <section className="login-story" aria-label="产品说明">
        <div className="brand-lockup">
          <span className="brand-mark"><Database size={22} /></span>
          <span>LazyTT Admin</span>
        </div>
        <div className="story-copy">
          <span className="eyebrow light">SHARED KNOWLEDGE CONTROL</span>
          <h1>让每一份企业知识，<br />都有清晰的归属。</h1>
          <p>统一管理租户共享与系统共享知识库，掌握文档处理状态与内容生命周期。</p>
        </div>
        <div className="scope-preview">
          <div><Building2 size={18} /><span>租户知识严格隔离</span></div>
          <div><Globe2 size={18} /><span>系统知识全局共享</span></div>
        </div>
      </section>

      <section className="login-panel">
        <form className="login-card" onSubmit={handleSubmit}>
          <div className="mobile-brand brand-lockup">
            <span className="brand-mark"><Database size={20} /></span>
            <span>LazyTT Admin</span>
          </div>
          <span className="eyebrow">ADMIN CONSOLE</span>
          <h2>进入知识管理后台</h2>
          <p className="login-subtitle">请选择本次管理范围并填写开发身份。</p>

          <div className="role-switch" role="radiogroup" aria-label="管理员类型">
            <button
              type="button"
              role="radio"
              aria-checked={role === 'TENANT_ADMIN'}
              className={role === 'TENANT_ADMIN' ? 'active' : ''}
              onClick={() => setRole('TENANT_ADMIN')}
            >
              <Building2 size={18} />
              <span><strong>租户管理员</strong><small>管理本企业知识</small></span>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={role === 'SYSTEM_ADMIN'}
              className={role === 'SYSTEM_ADMIN' ? 'active' : ''}
              onClick={() => setRole('SYSTEM_ADMIN')}
            >
              <Globe2 size={18} />
              <span><strong>系统管理员</strong><small>管理全局知识</small></span>
            </button>
          </div>

          {role === 'TENANT_ADMIN' && (
            <label className="field">
              <span>租户 ID</span>
              <input value={tenantId} onChange={(event) => setTenantId(event.target.value)} placeholder="例如 tenant-a" />
            </label>
          )}
          <label className="field">
            <span>用户 ID</span>
            <input value={userId} onChange={(event) => setUserId(event.target.value)} placeholder="例如 admin-001" />
          </label>

          {error && <p className="form-error">{error}</p>}
          <button className="primary-button login-button" type="submit">
            进入管理后台 <ChevronRight size={18} />
          </button>

          <div className="security-note">
            <ShieldAlert size={18} />
            <p><strong>开发环境身份模拟</strong>当前未接入 JWT 与管理员鉴权，请仅在本机或可信网络使用。</p>
          </div>
        </form>
      </section>
    </main>
  );
}

/**
 * 管理后台框架。角色决定唯一可管理的知识库范围，避免页面上出现越权范围切换入口。
 */
function AdminShell({ session, onLogout }: { session: AdminSession; onLogout: () => void }) {
  const isTenantAdmin = session.role === 'TENANT_ADMIN';
  const [section, setSection] = useState<AdminSection>('overview');

  return (
    <main className="admin-layout">
      <aside className="sidebar">
        <div className="brand-lockup sidebar-brand">
          <span className="brand-mark"><Database size={20} /></span>
          <span>LazyTT <b>Admin</b></span>
        </div>

        <nav className="main-nav" aria-label="管理导航">
          <p>工作台</p>
          <button className={section === 'overview' ? 'active' : ''} onClick={() => setSection('overview')}><LayoutDashboard size={18} />总览</button>
          <p>知识管理</p>
          <button className={section === 'knowledge' ? 'active' : ''} onClick={() => setSection('knowledge')}><BookOpen size={18} />共享知识库</button>
          <button className={section === 'documents' ? 'active' : ''} onClick={() => setSection('documents')}><FileStack size={18} />文档与任务</button>
        </nav>

        <div className="sidebar-session">
          <div className="sidebar-avatar">{session.userId.slice(0, 1).toUpperCase()}</div>
          <div><strong>{session.userId}</strong><span>{isTenantAdmin ? session.tenantId : '系统全局'}</span></div>
          <button onClick={onLogout} aria-label="退出当前身份"><LogOut size={17} /></button>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <span className={`scope-pill ${isTenantAdmin ? 'tenant' : 'system'}`}>
              {isTenantAdmin ? <Building2 size={14} /> : <Globe2 size={14} />}
              {isTenantAdmin ? '租户管理范围' : '系统管理范围'}
            </span>
            <span className="identity-copy">{isTenantAdmin ? session.tenantId : '全部租户可访问'}</span>
          </div>
          <div className="dev-badge"><ShieldAlert size={15} />开发身份模式</div>
        </header>

        <AdminWorkspace session={session} section={section} onSectionChange={setSection} />
      </section>
    </main>
  );
}

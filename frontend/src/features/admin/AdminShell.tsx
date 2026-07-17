import {
  BarChart3,
  Database,
  Download,
  FlaskConical,
  LogOut,
  Settings2,
  ShieldCheck,
} from "lucide-react";
import type { ReactNode } from "react";

import { ADMIN_SECTIONS, type AdminSectionKey } from "./adminTypes";

interface AdminShellProps {
  activeSection: AdminSectionKey;
  adminUser: string;
  onSelectSection: (section: AdminSectionKey) => void | Promise<void>;
  onLogout: () => void;
  children: ReactNode;
}

const SECTION_ICONS = {
  system: ShieldCheck,
  data: Database,
  export: Download,
  control: Settings2,
  analysis: BarChart3,
};

export function AdminShell({
  activeSection,
  adminUser,
  onSelectSection,
  onLogout,
  children,
}: AdminShellProps) {
  return (
    <main className="admin-dashboard">
      <aside className="admin-sidebar">
        <div className="admin-brand">
          <div className="admin-brand-mark">
            <FlaskConical size={18} />
          </div>
          <div>
            <strong>Dashboard</strong>
            <span>{adminUser}</span>
          </div>
        </div>
        <nav className="admin-nav" aria-label="Admin sections">
          {ADMIN_SECTIONS.map((section) => {
            const Icon = SECTION_ICONS[section.key];
            return (
              <button
                key={section.key}
                className={`admin-nav-button${
                  activeSection === section.key ? " admin-nav-button--active" : ""
                }`}
                type="button"
                disabled={section.disabled}
                onClick={() => onSelectSection(section.key)}
              >
                <Icon size={16} />
                <span>{section.label}</span>
                {section.disabled ? <em>后续</em> : null}
              </button>
            );
          })}
        </nav>
        <button className="admin-nav-button admin-nav-button--logout" type="button" onClick={onLogout}>
          <LogOut size={16} />
          <span>退出登录</span>
        </button>
      </aside>
      <section className="admin-content">{children}</section>
    </main>
  );
}

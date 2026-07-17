import { useCallback, useEffect, useRef, useState } from "react";

import { apiClient } from "../../api/client";
import { AdminLogin } from "./AdminLogin";
import { AdminShell } from "./AdminShell";
import type { AdminSectionKey } from "./adminTypes";
import { AnalysisPlaceholderSection } from "./sections/AnalysisPlaceholderSection";
import { ControlSection } from "./sections/ControlSection";
import { DataMonitorSection } from "./sections/DataMonitorSection";
import { ExportSection } from "./sections/ExportSection";
import { SystemMonitorSection } from "./sections/SystemMonitorSection";

type AuthState = "checking" | "unauthenticated" | "authenticated";
type UnsavedNavigationGuard = () => Promise<boolean>;

export function AdminApp() {
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [adminUser, setAdminUser] = useState("admin");
  const [activeSection, setActiveSection] = useState<AdminSectionKey>("system");
  const unsavedNavigationGuardRef = useRef<UnsavedNavigationGuard | null>(null);

  const registerUnsavedNavigationGuard = useCallback((guard: UnsavedNavigationGuard | null) => {
    unsavedNavigationGuardRef.current = guard;
  }, []);

  useEffect(() => {
    let mounted = true;
    void apiClient
      .getAdminSession()
      .then((session) => {
        if (!mounted) {
          return;
        }
        setAuthState(session.authenticated ? "authenticated" : "unauthenticated");
        if (session.admin_user) {
          setAdminUser(session.admin_user);
        }
      })
      .catch(() => {
        if (mounted) {
          setAuthState("unauthenticated");
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  const requestSectionChange = useCallback(
    async (nextSection: AdminSectionKey) => {
      if (nextSection === activeSection) {
        return;
      }
      const guard = unsavedNavigationGuardRef.current;
      if (guard) {
        const canLeave = await guard();
        if (!canLeave) {
          return;
        }
        unsavedNavigationGuardRef.current = null;
      }
      setActiveSection(nextSection);
    },
    [activeSection],
  );

  const logout = async () => {
    const guard = unsavedNavigationGuardRef.current;
    if (guard) {
      const canLeave = await guard();
      if (!canLeave) {
        return;
      }
      unsavedNavigationGuardRef.current = null;
    }
    try {
      await apiClient.adminLogout();
    } finally {
      setAuthState("unauthenticated");
      setActiveSection("system");
    }
  };

  if (authState === "checking") {
    return (
      <main className="admin-loading-page">
        <section className="admin-loading-panel">
          <p className="admin-kicker">Admin</p>
          <h1>正在确认登录状态</h1>
        </section>
      </main>
    );
  }

  if (authState === "unauthenticated") {
    return (
      <AdminLogin
        onAuthenticated={(nextAdminUser) => {
          setAdminUser(nextAdminUser);
          setAuthState("authenticated");
        }}
      />
    );
  }

  return (
    <AdminShell
      activeSection={activeSection}
      adminUser={adminUser}
      onSelectSection={(section) => void requestSectionChange(section)}
      onLogout={() => void logout()}
    >
      {activeSection === "system" ? <SystemMonitorSection /> : null}
      {activeSection === "data" ? <DataMonitorSection /> : null}
      {activeSection === "export" ? <ExportSection /> : null}
      {activeSection === "control" ? (
        <ControlSection registerUnsavedNavigationGuard={registerUnsavedNavigationGuard} />
      ) : null}
      {activeSection === "analysis" ? <AnalysisPlaceholderSection /> : null}
    </AdminShell>
  );
}

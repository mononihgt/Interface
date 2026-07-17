import { useEffect, useMemo, useRef, useState } from "react";

import { apiClient, ApiError } from "../api/client";
import type { ClientInfo, ParticipantView, SessionView } from "../api/types";
import { getDesktopGateState } from "../components/DesktopGate";
import { AdminApp } from "../features/admin/AdminApp";
import { runFormalEnvironmentGate } from "../features/environment/formalEnvironmentGate";
import { CompletePage, type CompletionViewMode } from "../pages/CompletePage";
import { ExperimentPage } from "../pages/ExperimentPage";
import { PretestPage } from "../pages/PretestPage";
import { TestChannelPage } from "../pages/TestChannelPage";
import { WelcomePage } from "../pages/WelcomePage";

type RouteMatch =
  | { name: "welcome" }
  | { name: "pretest" }
  | { name: "experiment"; sessionId: string }
  | { name: "complete" }
  | { name: "test" }
  | { name: "admin" };

type FormalExperimentEntryState = "idle" | "checking" | "ready" | "blocked";

function normalizePath(pathname: string): string {
  if (pathname === "/") {
    return "/welcome";
  }
  return pathname.replace(/\/+$/, "") || "/welcome";
}

function matchRoute(pathname: string): RouteMatch {
  const path = normalizePath(pathname);
  if (path === "/welcome") {
    return { name: "welcome" };
  }
  if (path === "/pretest") {
    return { name: "pretest" };
  }
  if (path === "/complete") {
    return { name: "complete" };
  }
  if (path === "/test") {
    return { name: "test" };
  }
  if (path === "/admin") {
    return { name: "admin" };
  }
  const experimentMatch = path.match(/^\/experiment\/([^/]+)$/);
  if (experimentMatch) {
    return { name: "experiment", sessionId: decodeURIComponent(experimentMatch[1]) };
  }
  return { name: "welcome" };
}

export function navigate(path: string, replace = false) {
  const nextPath = normalizePath(path);
  if (replace) {
    window.history.replaceState({}, "", nextPath);
  } else {
    window.history.pushState({}, "", nextPath);
  }
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function fallbackClientInfo(): ClientInfo {
  return {
    device_type: window.innerWidth >= 1024 ? "desktop" : window.innerWidth >= 768 ? "tablet" : "mobile",
    viewport_width: window.innerWidth,
    is_secure_context: window.isSecureContext,
    browser_name: "unknown",
    browser_version: null,
    microphone_available: Boolean(navigator.mediaDevices?.getUserMedia),
    microphone_permission: "prompt",
  };
}

export function AppRouter() {
  const [pathname, setPathname] = useState(() => normalizePath(window.location.pathname));
  const [participant, setParticipant] = useState<ParticipantView | null>(null);
  const [hasWelcomeParticipantSession, setHasWelcomeParticipantSession] = useState(false);
  const [session, setSession] = useState<SessionView | null>(null);
  const [formalGateClientInfo, setFormalGateClientInfo] = useState<ClientInfo | null>(null);
  const [hasBootstrappedParticipant, setHasBootstrappedParticipant] = useState(false);
  const [bootError, setBootError] = useState<string | null>(null);
  const [isEnteringFormalSession, setIsEnteringFormalSession] = useState(false);
  const [enterExperimentError, setEnterExperimentError] = useState<string | null>(null);
  const [completionMode, setCompletionMode] = useState<CompletionViewMode>("saved");
  const [formalExperimentEntryState, setFormalExperimentEntryState] =
    useState<FormalExperimentEntryState>("idle");
  const [formalExperimentEntryError, setFormalExperimentEntryError] = useState<string | null>(null);
  const enteringFormalSessionRef = useRef(false);

  useEffect(() => {
    const handlePopstate = () => {
      setPathname(normalizePath(window.location.pathname));
    };
    window.addEventListener("popstate", handlePopstate);
    return () => {
      window.removeEventListener("popstate", handlePopstate);
    };
  }, []);

  useEffect(() => {
    void getDesktopGateState()
      .then((state) => setFormalGateClientInfo(state.clientInfo))
      .catch(() => setFormalGateClientInfo(fallbackClientInfo()));
  }, []);

  useEffect(() => {
    const route = matchRoute(pathname);
    if (route.name === "experiment") {
      let cancelled = false;
      setBootError(null);
      setSession(null);
      setFormalExperimentEntryState("checking");
      setFormalExperimentEntryError(null);

      const restoreExperimentRoute = async () => {
        try {
          const restoredSession = await apiClient.getSession(route.sessionId);
          if (cancelled) {
            return;
          }
          setSession(restoredSession);

          if (restoredSession.is_test) {
            setFormalExperimentEntryState("ready");
            return;
          }

          if (restoredSession.status === "completed") {
            setCompletionMode("saved");
            navigate("/complete", true);
            void apiClient
              .me()
              .then(setParticipant)
              .catch((error: unknown) => {
                console.info("完成页参与者信息刷新失败。", error);
              });
            return;
          }

          const gate = await runFormalEnvironmentGate();
          if (cancelled) {
            return;
          }
          if (gate.clientInfo) {
            setFormalGateClientInfo(gate.clientInfo);
          }
          if (!gate.passed) {
            setFormalExperimentEntryError(
              gate.message ?? "当前环境不满足正式实验要求。",
            );
            setFormalExperimentEntryState("blocked");
            return;
          }

          const resumedSession = await apiClient.startSession({
            is_test: false,
            client_info: gate.clientInfo ?? fallbackClientInfo(),
          });
          if (cancelled) {
            return;
          }
          setSession(resumedSession);
          setFormalExperimentEntryState("ready");
          if (resumedSession.session_id !== route.sessionId) {
            navigate(`/experiment/${resumedSession.session_id}`, true);
          }
        } catch (error: unknown) {
          if (cancelled) {
            return;
          }
          if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
            setSession(null);
            setParticipant(null);
            setHasWelcomeParticipantSession(false);
            navigate(error.detail === "Admin login required." ? "/test" : "/welcome", true);
            return;
          }
          if (error instanceof ApiError && error.status === 400) {
            setFormalExperimentEntryError(error.detail);
            setFormalExperimentEntryState("blocked");
            return;
          }
          setBootError(error instanceof Error ? error.message : "无法加载实验会话。");
        }
      };

      void restoreExperimentRoute();
      return () => {
        cancelled = true;
      };
    }
    setSession(null);
    setFormalExperimentEntryState("idle");
    setFormalExperimentEntryError(null);
  }, [pathname]);

  const route = useMemo(() => matchRoute(pathname), [pathname]);
  const routeSessionIsLoaded =
    route.name !== "experiment" ||
    (session !== null && session.session_id === route.sessionId);
  const routeSessionIsTest =
    route.name === "experiment" &&
    session?.session_id === route.sessionId &&
    session?.is_test === true;
  const pretestGatePassed =
    route.name === "pretest" &&
    participant !== null &&
    (participant.participation_state === "completed" ||
      participant.current_day.day_index > 1 ||
      participant.pretest_status.has_final);
  const restorableParticipantRoutes =
    route.name === "pretest" ||
    (route.name === "experiment" && routeSessionIsLoaded && !routeSessionIsTest);
  const participantFlowRoutes =
    restorableParticipantRoutes && !pretestGatePassed;
  const isHydratingProtectedParticipantRoute =
    participantFlowRoutes &&
    !hasBootstrappedParticipant &&
    !participant;

  useEffect(() => {
    if (!restorableParticipantRoutes) {
      setBootError(null);
      setHasBootstrappedParticipant(true);
      return;
    }
    setBootError(null);
    setHasBootstrappedParticipant(false);
    void apiClient
      .me()
      .then(setParticipant)
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 401) {
          setParticipant(null);
          navigate("/welcome", true);
          return;
        }
        setBootError(error instanceof Error ? error.message : "初始化失败。");
      })
      .finally(() => {
        setHasBootstrappedParticipant(true);
      });
  }, [restorableParticipantRoutes, pathname]);

  const startFormalSession = async (
    showFailureAlert = true,
  ): Promise<{ started: boolean; message?: string | null }> => {
    const gate = await runFormalEnvironmentGate();
    if (gate.clientInfo) {
      setFormalGateClientInfo(gate.clientInfo);
    }
    if (!gate.passed) {
      if (showFailureAlert && gate.message) {
        window.alert(gate.message);
      }
      return { started: false, message: gate.message };
    }
    const nextSession = await apiClient.startSession({
      is_test: false,
      client_info: gate.clientInfo ?? fallbackClientInfo(),
    });
    setSession(nextSession);
    navigate(`/experiment/${nextSession.session_id}`);
    return { started: true };
  };

  useEffect(() => {
    if (!pretestGatePassed || !participant) {
      return;
    }

    let cancelled = false;
    const restoreFinalizedPretestRoute = async () => {
      setHasWelcomeParticipantSession(false);
      setEnterExperimentError(null);

      if (participant.participation_state === "completed") {
        setCompletionMode("saved");
        navigate("/complete", true);
        return;
      }

      if (!participant.current_day.can_start_experiment) {
        setEnterExperimentError(
          participant.participation_message ?? "当前实验状态暂时无法恢复。",
        );
        return;
      }

      setIsEnteringFormalSession(true);
      try {
        const startResult = await startFormalSession(false);
        if (!startResult.started && !cancelled) {
          setEnterExperimentError(
            startResult.message ?? "当前环境不满足正式实验要求。",
          );
        }
      } catch (error) {
        if (!cancelled) {
          setEnterExperimentError(
            error instanceof Error ? error.message : "恢复正式实验失败。",
          );
        }
      } finally {
        if (!cancelled) {
          setIsEnteringFormalSession(false);
        }
      }
    };

    void restoreFinalizedPretestRoute();
    return () => {
      cancelled = true;
    };
  }, [pretestGatePassed, participant?.attempt_id]);

  const handlePretestEnterExperiment = async () => {
    if (isEnteringFormalSession) {
      return;
    }
    if (enteringFormalSessionRef.current) {
      return;
    }

    enteringFormalSessionRef.current = true;
    setIsEnteringFormalSession(true);
    setEnterExperimentError(null);
    try {
      const nextParticipant = await apiClient.me();
      setParticipant(nextParticipant);
      setHasWelcomeParticipantSession(true);
      const startResult = await startFormalSession();
      if (!startResult.started) {
        const message = startResult.message ?? "当前环境不满足正式实验要求。";
        setEnterExperimentError(message);
        throw new Error(message);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "进入正式实验失败。";
      setEnterExperimentError(message);
      throw error instanceof Error ? error : new Error(message);
    } finally {
      enteringFormalSessionRef.current = false;
      setIsEnteringFormalSession(false);
    }
  };

  const formalStartDisabled =
    !participant ||
    !participant.current_day.can_start_experiment ||
    !formalGateClientInfo ||
    formalGateClientInfo.device_type !== "desktop" ||
    formalGateClientInfo.viewport_width < 1024 ||
    !formalGateClientInfo.is_secure_context ||
    !["chrome", "edge"].includes(formalGateClientInfo.browser_name) ||
    !formalGateClientInfo.microphone_available ||
    formalGateClientInfo.microphone_permission !== "granted";
  const welcomeParticipant = hasWelcomeParticipantSession ? participant : null;

  if (bootError) {
    return (
      <main className="app-shell experiment-app-shell">
        <section className="page-grid">
          <div className="panel hero-panel">
            <div className="panel-heading">
              <div>
                <p className="panel-kicker">Frontend Boot</p>
                <h1>初始化失败</h1>
              </div>
            </div>
            <p className="status-inline status-inline--error">{bootError}</p>
          </div>
        </section>
      </main>
    );
  }

  if (isHydratingProtectedParticipantRoute) {
    return (
      <main className="app-shell experiment-app-shell">
        <section className="page-grid">
          <div className="panel hero-panel">
            <p className="panel-kicker">Participant Session</p>
            <h1>正在确认登录状态</h1>
          </div>
        </section>
      </main>
    );
  }

  switch (route.name) {
    case "pretest":
      if (pretestGatePassed) {
        return (
          <main className="app-shell experiment-app-shell">
            <section className="page-grid">
              <div className="panel hero-panel">
                <p className="panel-kicker">Participant Session</p>
                <h1>{enterExperimentError ? "当前无法恢复实验" : "正在恢复实验"}</h1>
                {enterExperimentError ? (
                  <p className="status-inline status-inline--error">{enterExperimentError}</p>
                ) : null}
              </div>
            </section>
          </main>
        );
      }
      return (
        <PretestPage
          onEnterExperiment={handlePretestEnterExperiment}
          enterExperimentError={enterExperimentError}
        />
      );
    case "experiment":
      if (
        session &&
        !session.is_test &&
        formalExperimentEntryState === "blocked"
      ) {
        return (
          <main className="app-shell experiment-app-shell">
            <section className="page-grid">
              <div className="panel hero-panel">
                <p className="panel-kicker">Formal Session</p>
                <h1>当前环境无法进入实验</h1>
                <p className="status-inline status-inline--error">
                  {formalExperimentEntryError ?? "当前环境不满足正式实验要求。"}
                </p>
              </div>
            </section>
          </main>
        );
      }
      return session && (session.is_test || formalExperimentEntryState === "ready") ? (
        <ExperimentPage
          session={session}
          onSessionChange={setSession}
          onComplete={async () => {
            setCompletionMode("saved");
            try {
              const nextParticipant = await apiClient.me();
              setParticipant(nextParticipant);
            } catch (error) {
              console.info("完成页参与者信息刷新失败。", error);
            } finally {
              navigate("/complete");
            }
          }}
        />
      ) : (
        <main className="app-shell experiment-app-shell">
          <section className="page-grid">
            <div className="panel hero-panel">
              <p className="panel-kicker">Formal Session</p>
              <h1>正在加载会话</h1>
            </div>
          </section>
        </main>
      );
    case "complete":
      return (
        <CompletePage
          participant={participant}
          mode={participant ? completionMode : "test"}
        />
      );
    case "test":
      return <TestChannelPage onBack={() => navigate("/welcome")} />;
    case "admin":
      return <AdminApp />;
    case "welcome":
    default:
      return (
        <WelcomePage
          participant={welcomeParticipant}
          onLoginSuccess={(nextParticipant) => {
            setParticipant(nextParticipant);
            setHasWelcomeParticipantSession(true);
          }}
          onBeforeLogin={async () => {
            const gate = await runFormalEnvironmentGate();
            if (gate.clientInfo) {
              setFormalGateClientInfo(gate.clientInfo);
            }
            return {
              passed: gate.passed,
              message: gate.message,
            };
          }}
          onGoPretest={() => navigate("/pretest")}
          onStartFormal={() => void startFormalSession()}
          onGoTest={() => navigate("/test")}
          formalStartDisabled={formalStartDisabled}
        />
      );
  }
}

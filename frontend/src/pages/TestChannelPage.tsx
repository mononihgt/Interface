import { FlaskConical } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { apiClient, ApiError } from "../api/client";
import type {
  Condition,
  Subcondition,
  TestScenarioControls,
} from "../api/types";
import { getDesktopGateState } from "../components/DesktopGate";
import { TestLoginPage } from "./TestLoginPage";

interface TestChannelPageProps {
  onBack: () => void;
}

type AuthState = "checking" | "unauthenticated" | "authenticated";

const DEFAULT_TEST_SCENARIO: TestScenarioControls = {
  condition: "human",
  subcondition: "qa",
  topic_key: "advice",
  error_type_id: "factual_minor",
  planned_error_turn: 2,
};

const TEST_SCENARIO_STORAGE_KEY = "interface_v2_last_test_scenario";

type TopicOption = readonly [string, string];

function cellKey(condition: Condition, subcondition: Subcondition) {
  return `${condition}:${subcondition}`;
}

const TOPIC_OPTIONS_BY_CELL: Record<string, readonly TopicOption[]> = {
  [cellKey("tool", "qa")]: [
    ["weather", "天气"],
    ["physics", "物理常识"],
  ],
  [cellKey("tool", "planning")]: [
    ["travelPlan", "旅游规划"],
    ["hiking", "短期踏青"],
  ],
  [cellKey("tool", "chat")]: [
    ["news", "新闻热点"],
    ["tech", "科技资讯"],
  ],
  [cellKey("tool", "decision")]: [["valueDecision", "价值导向决策"]],
  [cellKey("tool", "execution")]: [["taskExecution", "明确任务执行"]],
  [cellKey("human", "qa")]: [["advice", "咨询建议"]],
  [cellKey("human", "planning")]: [["goalPlan", "目标规划"]],
  [cellKey("human", "chat")]: [["funStory", "趣事分享"]],
  [cellKey("human", "decision")]: [["preferenceDecision", "主观偏好决策"]],
  [cellKey("human", "execution")]: [["collaborativeExecution", "协作任务执行"]],
};

const ERROR_OPTIONS = [
  ["factual_minor", "事实性轻微错误"],
  ["factual_major", "事实性严重错误"],
  ["logic_minor", "逻辑性轻微错误"],
  ["logic_major", "逻辑性严重错误"],
  ["social_minor", "社会性轻微错误"],
  ["social_major", "社会性严重错误"],
  ["system_failure", "系统失败"],
] as const;

function readStoredScenario(): TestScenarioControls {
  try {
    const raw = window.sessionStorage.getItem(TEST_SCENARIO_STORAGE_KEY);
    return raw ? Object.assign({}, DEFAULT_TEST_SCENARIO, JSON.parse(raw)) : DEFAULT_TEST_SCENARIO;
  } catch {
    return DEFAULT_TEST_SCENARIO;
  }
}

function persistStoredScenario(scenario: TestScenarioControls) {
  try {
    window.sessionStorage.setItem(TEST_SCENARIO_STORAGE_KEY, JSON.stringify(scenario));
  } catch {
    return;
  }
}

function navigateInCurrentTab(path: string) {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function TestChannelPage({ onBack }: TestChannelPageProps) {
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [scenarioControls, setScenarioControls] = useState<TestScenarioControls>(
    () => readStoredScenario(),
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const availableTopicOptions = useMemo(
    () =>
      TOPIC_OPTIONS_BY_CELL[
        cellKey(scenarioControls.condition, scenarioControls.subcondition)
      ] ?? [],
    [scenarioControls.condition, scenarioControls.subcondition],
  );

  useEffect(() => {
    let isMounted = true;
    void apiClient
      .getAdminOverview()
      .then(() => {
        if (isMounted) {
          setAuthState("authenticated");
        }
      })
      .catch((error: unknown) => {
        if (!isMounted) {
          return;
        }
        if (error instanceof ApiError && error.status === 401) {
          setAuthState("unauthenticated");
          return;
        }
        setAuthState("unauthenticated");
        setErrorMessage(error instanceof Error ? error.message : "测试通道初始化失败。");
      });
    return () => {
      isMounted = false;
    };
  }, []);

  useEffect(() => {
    if (
      availableTopicOptions.length &&
      !availableTopicOptions.some(([value]) => value === scenarioControls.topic_key)
    ) {
      setScenarioControls((current) => ({
        ...current,
        topic_key: availableTopicOptions[0][0],
      }));
    }
  }, [availableTopicOptions, scenarioControls.topic_key]);

  const startTestSession = async () => {
    setErrorMessage(null);
    setIsStarting(true);
    try {
      const gateState = await getDesktopGateState();
      const nextSession = await apiClient.startTestSession({
        is_test: true,
        client_info: gateState.clientInfo,
        ...scenarioControls,
      });
      persistStoredScenario(scenarioControls);
      navigateInCurrentTab(`/experiment/${nextSession.session_id}`);
    } catch (error) {
      setErrorMessage(
        error instanceof ApiError ? error.detail : "测试会话启动失败。",
      );
    } finally {
      setIsStarting(false);
    }
  };

  if (authState === "checking") {
    return (
      <main className="flow-page">
        <section className="flow-card flow-card--test">
          <h1 className="flow-title">正在确认测试通道权限</h1>
        </section>
      </main>
    );
  }

  if (authState === "unauthenticated") {
    return (
      <TestLoginPage
        onAuthenticated={() => {
          setErrorMessage(null);
          setAuthState("authenticated");
        }}
        onBack={onBack}
      />
    );
  }

  return (
    <main className="flow-page">
      <section className="flow-card flow-card--test test-control-card">
        <h1 className="flow-title">测试通道</h1>
        <p className="flow-message">
          测试通道使用管理员鉴权。所有测试会话均强制写入
          <code>is_test=true</code>，不会占用正式被试当日状态或正式分配计数。
        </p>
          <div className="test-controls-grid">
            <label className="field">
              <span>实验条件</span>
              <select
                value={scenarioControls.condition}
                onChange={(event) =>
                  setScenarioControls((current) => ({
                    ...current,
                    condition: event.target.value as TestScenarioControls["condition"],
                  }))
                }
              >
                <option value="human">human</option>
                <option value="tool">tool</option>
              </select>
            </label>
            <label className="field">
              <span>实验子条件</span>
              <select
                value={scenarioControls.subcondition}
                onChange={(event) =>
                  setScenarioControls((current) => ({
                    ...current,
                    subcondition: event.target.value as TestScenarioControls["subcondition"],
                  }))
                }
              >
                <option value="qa">qa</option>
                <option value="planning">planning</option>
                <option value="chat">chat</option>
                <option value="decision">decision</option>
                <option value="execution">execution</option>
              </select>
            </label>
            <label className="field">
              <span>任务主题</span>
              <select
                value={scenarioControls.topic_key}
                onChange={(event) =>
                  setScenarioControls((current) => ({
                    ...current,
                    topic_key: event.target.value,
                  }))
                }
              >
                {availableTopicOptions.map(([value, label]) => (
                  <option value={value} key={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>错误类型</span>
              <select
                value={scenarioControls.error_type_id}
                onChange={(event) =>
                  setScenarioControls((current) => ({
                    ...current,
                    error_type_id: event.target.value,
                  }))
                }
              >
                {ERROR_OPTIONS.map(([value, label]) => (
                  <option value={value} key={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>计划错误轮次</span>
              <select
                value={scenarioControls.planned_error_turn}
                onChange={(event) =>
                  setScenarioControls((current) => ({
                    ...current,
                    planned_error_turn: Number(event.target.value),
                  }))
                }
              >
                {[1, 2, 3, 4, 5].map((value) => (
                  <option value={value} key={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {errorMessage ? (
            <p className="status-inline status-inline--error">{errorMessage}</p>
          ) : null}
        <div className="flow-actions">
          <button className="secondary-button" type="button" onClick={onBack}>
            返回入口
          </button>
          <button
            className="primary-button test-button"
            type="button"
            onClick={() => void startTestSession()}
            disabled={isStarting}
          >
            <FlaskConical size={16} aria-hidden="true" />
            <span>{isStarting ? "启动中" : "启动测试会话"}</span>
          </button>
        </div>
      </section>
    </main>
  );
}

import { Play, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { apiClient } from "../../../api/client";
import type {
  AdminSystemMetricsView,
  ApiHealthSummaryView,
  DeepSeekTestResultView,
  ProviderModelUsageView,
  ProviderUsageRow,
  SystemLogsSummaryView,
} from "../../../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { MetricCard } from "../components/MetricCard";
import { StatusBadge, statusTone } from "../components/StatusBadge";
import { formatBytes, formatDateTime, formatNumber, formatPercent, formatStatusLabel } from "../adminTypes";

interface SystemMonitorState {
  metrics: AdminSystemMetricsView | null;
  usage: ProviderModelUsageView | null;
  health: ApiHealthSummaryView | null;
  logs: SystemLogsSummaryView | null;
}

const usageColumns: DataTableColumn<ProviderUsageRow>[] = [
  {
    key: "provider",
    header: "服务商 / 模型",
    render: (row) => (
      <span>
        {row.provider}
        <small className="admin-muted-cell">{row.model ?? "-"}</small>
      </span>
    ),
  },
  { key: "calls", header: "调用", render: (row) => formatNumber(row.calls) },
  { key: "successes", header: "成功", render: (row) => formatNumber(row.successes) },
  { key: "failures", header: "失败", render: (row) => formatNumber(row.failures) },
  { key: "timeout_count", header: "超时", render: (row) => formatNumber(row.timeout_count) },
  { key: "success_rate", header: "成功率", render: (row) => formatPercent(row.success_rate) },
  {
    key: "latency",
    header: "延迟",
    render: (row) => `${row.avg_latency_ms ?? "-"} / ${row.p95_latency_ms ?? "-"} ms`,
  },
  {
    key: "cooldown",
    header: "冷却次数",
    render: (row) => formatNumber(row.cooldown_applied_count),
  },
  {
    key: "last_called_at",
    header: "最近调用",
    render: (row) => formatDateTime(row.last_called_at),
  },
  {
    key: "last_failure",
    header: "最近失败",
    render: (row) => row.last_failure_summary ?? row.last_failure_code ?? "-",
  },
];

function unknownText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  return String(value);
}

export function SystemMonitorSection() {
  const [state, setState] = useState<SystemMonitorState>({
    metrics: null,
    usage: null,
    health: null,
    logs: null,
  });
  const [isLoading, setIsLoading] = useState(true);
  const [isTestingDeepSeek, setIsTestingDeepSeek] = useState(false);
  const [testResult, setTestResult] = useState<DeepSeekTestResultView | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const load = async () => {
    setIsLoading(true);
    setErrorMessage(null);
    try {
      const [metrics, usage, health, logs] = await Promise.all([
        apiClient.getAdminSystemMetrics(),
        apiClient.getAdminProviderModelUsage(),
        apiClient.getAdminApiHealth(),
        apiClient.getAdminSystemLogs(),
      ]);
      setState({ metrics, usage, health, logs });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "系统监控加载失败。");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const testDeepSeek = async () => {
    setIsTestingDeepSeek(true);
    setErrorMessage(null);
    try {
      const result = await apiClient.testAdminDeepSeek();
      setTestResult(result);
      const [usage, health] = await Promise.all([
        apiClient.getAdminProviderModelUsage(),
        apiClient.getAdminApiHealth(),
      ]);
      setState((current) => ({ ...current, usage, health }));
    } catch (error) {
      setTestResult(null);
      setErrorMessage(error instanceof Error ? error.message : "DeepSeek 测试失败。");
    } finally {
      setIsTestingDeepSeek(false);
    }
  };

  const metrics = state.metrics;
  const logs = state.logs;
  const health = state.health;
  const allTimeWindow = state.usage?.windows.find((window) => window.window === "all_time");
  const last24hWindow = state.usage?.windows.find((window) => window.window === "last_24h");

  return (
    <div className="admin-section-stack">
      <header className="admin-section-header">
        <div>
          <p className="admin-kicker">System Monitor</p>
          <h1>系统监控</h1>
        </div>
        <button className="admin-secondary-button" type="button" onClick={() => void load()} disabled={isLoading}>
          <RefreshCw size={16} />
          <span>{isLoading ? "刷新中" : "刷新"}</span>
        </button>
      </header>

      {errorMessage ? <p className="admin-inline-error">{errorMessage}</p> : null}

      <div className="admin-metric-grid">
        <MetricCard label="服务状态" value={metrics?.service.status ?? "-"} detail={metrics?.service.label} tone="good" />
        <MetricCard label="数据库大小" value={formatBytes(metrics?.database.size_bytes)} detail={metrics?.database.path} />
        <MetricCard label="数据目录磁盘" value={formatBytes(metrics?.data_directory.disk_usage.used_bytes)} detail={`${formatBytes(metrics?.data_directory.disk_usage.free_bytes)} 可用`} />
        <MetricCard label="音频文件" value={formatNumber(metrics?.audio_directory.files)} detail={formatBytes(metrics?.audio_directory.size_bytes)} />
        <MetricCard label="导出文件" value={formatNumber(metrics?.exports_directory.files)} detail={formatBytes(metrics?.exports_directory.size_bytes)} />
        <MetricCard label="今日开始 / 完成" value={`${formatNumber(metrics?.experiment.today_started)} / ${formatNumber(metrics?.experiment.today_completed)}`} />
        <MetricCard label="需复核记录" value={formatNumber(metrics?.experiment.risk_sessions)} tone={metrics?.experiment.risk_sessions ? "warning" : "default"} />
        <MetricCard label="接口 / 语音识别失败" value={`${formatNumber(metrics?.experiment.api_failures)} / ${formatNumber(metrics?.experiment.asr_failures)}`} tone={metrics?.experiment.api_failures ? "danger" : "default"} />
      </div>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">DeepSeek Provider Test</p>
            <h2>DeepSeek 配置与测试</h2>
          </div>
          <button
            className="admin-secondary-button"
            type="button"
            onClick={() => void testDeepSeek()}
            disabled={isTestingDeepSeek}
          >
            <Play size={16} />
            <span>{isTestingDeepSeek ? "测试中" : "测试 DeepSeek"}</span>
          </button>
        </div>
        <div className="admin-split-grid">
          <div>
            <h3>服务端配置</h3>
            {state.usage ? (
              <>
                <p>
                  状态：
                  <StatusBadge tone={state.usage.deepseek_configuration.status === "configured" ? "good" : "warning"}>
                    {formatStatusLabel(state.usage.deepseek_configuration.status)}
                  </StatusBadge>
                </p>
                <p>模型：{state.usage.deepseek_configuration.model}</p>
                <p>超时：{state.usage.deepseek_configuration.timeout_seconds} 秒</p>
              </>
            ) : (
              <p className="admin-panel-note">-</p>
            )}
          </div>
          <div>
            <h3>最近测试</h3>
            {testResult ? (
              <>
                <p>状态：{formatStatusLabel(testResult.status)}</p>
                <p>服务商：{testResult.provider}</p>
                <p>模型：{testResult.model}</p>
                <p>延迟：{testResult.latency_ms === null ? "-" : `${testResult.latency_ms} ms`}</p>
                {testResult.error_code ? <p>错误代码：{testResult.error_code}</p> : null}
              </>
            ) : (
              <p className="admin-panel-note">-</p>
            )}
          </div>
        </div>
      </section>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">Provider Usage</p>
            <h2>服务商与模型调用统计</h2>
          </div>
        </div>
        <div className="admin-split-grid">
          <div>
            <h3>全部累计</h3>
            <DataTable
              columns={usageColumns}
              rows={allTimeWindow?.provider_model_rows ?? []}
              getRowKey={(row) => `${row.provider}:${row.model ?? "none"}`}
            />
          </div>
          <div>
            <h3>最近 24 小时</h3>
            <DataTable
              columns={usageColumns}
              rows={last24hWindow?.provider_model_rows ?? []}
              getRowKey={(row) => `${row.provider}:${row.model ?? "none"}`}
            />
          </div>
        </div>
      </section>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">API Health</p>
            <h2>接口健康状态</h2>
          </div>
        </div>
        <DataTable
          rows={health?.routes ?? []}
          getRowKey={(row, index) => `${unknownText(row.route)}:${unknownText(row.provider)}:${index}`}
          columns={[
            { key: "route", header: "路由", render: (row) => unknownText(row.route) },
            { key: "provider", header: "服务商", render: (row) => unknownText(row.provider) },
            { key: "model", header: "模型", render: (row) => unknownText(row.model) },
            { key: "total", header: "调用", render: (row) => unknownText(row.total) },
            {
              key: "success_rate",
              header: "成功率",
              render: (row) => formatPercent(typeof row.success_rate === "number" ? row.success_rate : null),
            },
            { key: "p95", header: "P95", render: (row) => unknownText(row.p95_latency_ms) },
          ]}
        />
      </section>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">System Logs</p>
            <h2>日志摘要</h2>
          </div>
          {logs?.sanitized_package_path ? (
            <StatusBadge tone="info">已生成脱敏包</StatusBadge>
          ) : null}
        </div>
        <div className="admin-split-grid admin-split-grid--three">
          <DataTable
            rows={Object.entries(logs?.backend_status_counts ?? {})}
            getRowKey={(row) => row[0]}
            columns={[
              { key: "status", header: "后端状态", render: (row) => <StatusBadge tone={statusTone(row[0])}>{formatStatusLabel(row[0])}</StatusBadge> },
              { key: "count", header: "数量", render: (row) => formatNumber(row[1]) },
            ]}
          />
          <DataTable
            rows={logs?.api_log_counts ?? []}
            getRowKey={(row, index) => `${row.route}:${row.status}:${index}`}
            columns={[
              { key: "route", header: "路由", render: (row) => row.route },
              { key: "status", header: "状态", render: (row) => <StatusBadge tone={statusTone(row.status)}>{formatStatusLabel(row.status)}</StatusBadge> },
              { key: "count", header: "数量", render: (row) => formatNumber(row.count) },
            ]}
          />
          <DataTable
            rows={Object.entries(logs?.asr_status_counts ?? {})}
            getRowKey={(row) => row[0]}
            columns={[
              { key: "status", header: "语音识别状态", render: (row) => <StatusBadge tone={statusTone(row[0])}>{formatStatusLabel(row[0])}</StatusBadge> },
              { key: "count", header: "数量", render: (row) => formatNumber(row[1]) },
            ]}
          />
        </div>
      </section>
    </div>
  );
}

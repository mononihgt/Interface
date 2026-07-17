import { Search, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { apiClient } from "../../../api/client";
import type {
  AdminDataMetricsView,
  AdminMonitorSessionRow,
  AdminParticipantRow,
  AdminParticipantsView,
} from "../../../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { MetricCard } from "../components/MetricCard";
import { StatusBadge, statusTone } from "../components/StatusBadge";
import {
  formatCondition,
  formatDateTime,
  formatErrorType,
  formatNumber,
  formatParticipantType,
  formatRiskFlag,
  formatStatusLabel,
  formatSubcondition,
} from "../adminTypes";

const sessionColumns: DataTableColumn<AdminMonitorSessionRow>[] = [
  { key: "participant", header: "被试", render: (row) => `${row.name} · ${row.masked_phone}` },
  { key: "type", header: "类型", render: (row) => formatParticipantType(row.participant_type) },
  { key: "cell", header: "条件", render: (row) => `${formatCondition(row.condition)} / ${formatSubcondition(row.subcondition)}` },
  { key: "topic", header: "主题", render: (row) => row.topic_key ?? "-" },
  { key: "error", header: "错误类型", render: (row) => formatErrorType(row.error_type_id) },
  { key: "day", header: "天次", render: (row) => formatNumber(row.day_index) },
  {
    key: "status",
    header: "状态",
    render: (row) => <StatusBadge tone={statusTone(row.session_status)}>{formatStatusLabel(row.session_status)}</StatusBadge>,
  },
  { key: "updated", header: "更新", render: (row) => formatDateTime(row.updated_at) },
];

const reviewSessionColumns: DataTableColumn<AdminMonitorSessionRow>[] = [
  ...sessionColumns,
  {
    key: "risk",
    header: "复核原因",
    render: (row) => row.risk_flags?.map(formatRiskFlag).join(", ") || "-",
  },
];

const participantColumns: DataTableColumn<AdminParticipantRow>[] = [
  { key: "id", header: "ID", render: (row) => row.participant_id },
  { key: "name", header: "姓名", render: (row) => row.name },
  { key: "phone", header: "手机", render: (row) => row.masked_phone },
  { key: "type", header: "类型", render: (row) => formatParticipantType(row.participant_type) },
  { key: "cell", header: "条件", render: (row) => `${formatCondition(row.condition)} / ${formatSubcondition(row.subcondition)}` },
  { key: "topic", header: "主题", render: (row) => row.topic_key },
  { key: "error", header: "错误类型", render: (row) => formatErrorType(row.error_type_id) },
  {
    key: "status",
    header: "状态",
    render: (row) => <StatusBadge tone={statusTone(row.current_status)}>{formatStatusLabel(row.current_status)}</StatusBadge>,
  },
  { key: "created", header: "创建", render: (row) => formatDateTime(row.created_at) },
];

export function DataMonitorSection() {
  const [dataMetrics, setDataMetrics] = useState<AdminDataMetricsView | null>(null);
  const [participants, setParticipants] = useState<AdminParticipantsView | null>(null);
  const [query, setQuery] = useState("");
  const [hasSearched, setHasSearched] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSearching, setIsSearching] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [searchErrorMessage, setSearchErrorMessage] = useState<string | null>(null);

  const loadMetrics = async () => {
    setIsLoading(true);
    setErrorMessage(null);
    try {
      const metrics = await apiClient.getAdminDataMetrics();
      setDataMetrics(metrics);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "数据监控加载失败。");
    } finally {
      setIsLoading(false);
    }
  };

  const searchParticipants = async () => {
    const nextQuery = query.trim();
    setSearchErrorMessage(null);
    if (!nextQuery) {
      setHasSearched(false);
      setParticipants(null);
      return;
    }

    setHasSearched(true);
    setIsSearching(true);
    try {
      setParticipants(await apiClient.getAdminParticipants(nextQuery));
    } catch (error) {
      setSearchErrorMessage(error instanceof Error ? error.message : "被试搜索失败。");
    } finally {
      setIsSearching(false);
    }
  };

  useEffect(() => {
    void loadMetrics();
  }, []);

  const metrics = dataMetrics?.metrics;

  return (
    <div className="admin-section-stack">
      <header className="admin-section-header">
        <div>
          <p className="admin-kicker">Data Monitor</p>
          <h1>数据监控</h1>
        </div>
        <button className="admin-secondary-button" type="button" onClick={() => void loadMetrics()} disabled={isLoading}>
          <RefreshCw size={16} />
          <span>{isLoading ? "刷新中" : "刷新"}</span>
        </button>
      </header>

      {errorMessage ? <p className="admin-inline-error">{errorMessage}</p> : null}

      <div className="admin-metric-grid">
        <MetricCard label="总被试数" value={formatNumber(metrics?.total_participants)} />
        <MetricCard label="今日开始" value={formatNumber(metrics?.today_started)} />
        <MetricCard label="今日完成" value={formatNumber(metrics?.today_completed)} />
        <MetricCard label="进行中记录" value={formatNumber(metrics?.active_sessions)} />
        <MetricCard label="已完成记录" value={formatNumber(metrics?.completed_sessions)} />
        <MetricCard label="短程 / 长程完成" value={`${formatNumber(metrics?.short_completed)} / ${formatNumber(metrics?.long_completed)}`} />
        <MetricCard label="需复核记录" value={formatNumber(metrics?.risk_sessions)} tone={metrics?.risk_sessions ? "warning" : "default"} />
        <MetricCard label="完整数据审核" value={`${formatNumber(metrics?.clean_data_eligible)} / ${formatNumber(metrics?.clean_data_review_needed)} / ${formatNumber(metrics?.clean_data_excluded)}`} detail="合格 / 需复核 / 已排除" />
        <MetricCard label="接口 / 语音识别失败" value={`${formatNumber(metrics?.api_failures)} / ${formatNumber(metrics?.asr_failures)}`} tone={metrics?.api_failures ? "danger" : "default"} />
      </div>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">Participants</p>
            <h2>被试搜索</h2>
          </div>
          <form
            className="admin-inline-form"
            onSubmit={(event) => {
              event.preventDefault();
              void searchParticipants();
            }}
          >
            <input
              value={query}
              placeholder="ID / 姓名 / 手机后四位"
              onChange={(event) => {
                setQuery(event.target.value);
                if (!event.target.value.trim()) {
                  setHasSearched(false);
                  setParticipants(null);
                  setSearchErrorMessage(null);
                }
              }}
            />
            <button className="admin-secondary-button" type="submit" disabled={isSearching}>
              <Search size={16} />
              <span>{isSearching ? "搜索中" : "搜索"}</span>
            </button>
          </form>
        </div>
        {searchErrorMessage ? <p className="admin-inline-error">{searchErrorMessage}</p> : null}
        {!hasSearched ? (
          <p className="admin-panel-note">请输入搜索条件后显示被试信息。</p>
        ) : (
          <DataTable
            columns={participantColumns}
            rows={participants?.items ?? []}
            getRowKey={(row) => row.participant_id}
            emptyLabel="没有匹配的被试"
          />
        )}
      </section>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">Attention</p>
            <h2>未完成与需复核列表</h2>
            <p className="admin-panel-note">包含 API/ASR 失败、缺少评分、系统标记异常或中途放弃的实验记录。</p>
          </div>
        </div>
        <div className="admin-review-list">
          <div className="admin-review-table-block">
            <h3>未完成记录</h3>
            <DataTable
              columns={reviewSessionColumns}
              rows={dataMetrics?.incomplete_sessions ?? []}
              getRowKey={(row) => row.session_id}
              emptyLabel="暂无未完成记录"
            />
          </div>
          <div className="admin-review-table-block">
            <h3>需复核记录</h3>
            <DataTable
              columns={reviewSessionColumns}
              rows={dataMetrics?.risk_sessions ?? []}
              getRowKey={(row) => row.session_id}
              emptyLabel="暂无需复核记录"
            />
          </div>
        </div>
      </section>
    </div>
  );
}

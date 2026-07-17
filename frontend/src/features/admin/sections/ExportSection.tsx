import { Download, RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { apiClient } from "../../../api/client";
import type { CleanDataAuditRow, CleanDataAuditsView, ExportJobView } from "../../../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { StatusBadge, statusTone } from "../components/StatusBadge";
import {
  formatCondition,
  formatDateTime,
  formatErrorType,
  formatExportType,
  formatNumber,
  formatParticipantType,
  formatStatusLabel,
  formatSubcondition,
} from "../adminTypes";

function formatJobDateRange(filters: Record<string, unknown>): string {
  const startDate = typeof filters.start_date === "string" ? filters.start_date : "";
  const endDate = typeof filters.end_date === "string" ? filters.end_date : "";
  if (!startDate && !endDate) {
    return "全部日期";
  }
  return `${startDate || "不限"} 至 ${endDate || "不限"}`;
}

const auditColumns: DataTableColumn<CleanDataAuditRow>[] = [
  { key: "participant", header: "被试", render: (row) => `${row.name} · #${row.participant_id}` },
  { key: "attempt", header: "尝试", render: (row) => row.attempt_id ?? "-" },
  { key: "type", header: "类型", render: (row) => formatParticipantType(row.participant_type) },
  { key: "cell", header: "条件", render: (row) => `${formatCondition(row.condition)} / ${formatSubcondition(row.subcondition)}` },
  { key: "error", header: "错误类型", render: (row) => formatErrorType(row.error_type_id) },
  {
    key: "status",
    header: "状态",
    render: (row) => <StatusBadge tone={statusTone(row.status)}>{formatStatusLabel(row.status)}</StatusBadge>,
  },
  { key: "reasons", header: "原因", render: (row) => row.reasons.join(", ") || "-" },
  { key: "computed", header: "更新时间", render: (row) => formatDateTime(row.computed_at) },
];

export function ExportSection() {
  const [jobs, setJobs] = useState<ExportJobView[]>([]);
  const [audits, setAudits] = useState<CleanDataAuditsView | null>(null);
  const [includeTest, setIncludeTest] = useState(false);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [auditStatus, setAuditStatus] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState<string | null>(null);
  const [deletingJobUuid, setDeletingJobUuid] = useState<string | null>(null);
  const [isUpdatingAudits, setIsUpdatingAudits] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const runningJobs = useMemo(
    () => jobs.some((job) => ["queued", "running"].includes(job.status)),
    [jobs],
  );
  const dateRangeInvalid = Boolean(startDate && endDate && startDate > endDate);

  const load = async () => {
    setIsLoading(true);
    setErrorMessage(null);
    try {
      const [jobPayload, auditPayload] = await Promise.all([
        apiClient.listAdminExportJobs(),
        apiClient.getAdminCleanDataAudits(auditStatus),
      ]);
      setJobs(jobPayload.items);
      setAudits(auditPayload);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "数据导出加载失败。");
    } finally {
      setIsLoading(false);
    }
  };

  const createJob = async (
    exportType: "experiment_data" | "complete_no_external_error_data" | "reimbursement",
  ) => {
    if (dateRangeInvalid) {
      setErrorMessage("开始日期不能晚于结束日期。");
      return;
    }
    setIsCreating(exportType);
    setErrorMessage(null);
    const filters: Record<string, unknown> = {};
    if (startDate) {
      filters.start_date = startDate;
    }
    if (endDate) {
      filters.end_date = endDate;
    }
    try {
      await apiClient.createAdminExportJob({
        export_type: exportType,
        include_test: exportType === "experiment_data" ? includeTest : false,
        filters,
      });
      const jobPayload = await apiClient.listAdminExportJobs();
      setJobs(jobPayload.items);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "导出作业创建失败。");
    } finally {
      setIsCreating(null);
    }
  };

  const updateAudits = async () => {
    setIsUpdatingAudits(true);
    setErrorMessage(null);
    try {
      const result = await apiClient.recomputeAdminCleanDataAudits();
      setAudits(result);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "完整数据审核更新失败。");
    } finally {
      setIsUpdatingAudits(false);
    }
  };

  const deleteJob = async (job: ExportJobView) => {
    const canDelete = !["queued", "running"].includes(job.status);
    if (!canDelete) {
      return;
    }
    const confirmed = window.confirm(
      `删除作业 ${formatExportType(job.export_type)}？这会同时删除服务器上的导出文件。`,
    );
    if (!confirmed) {
      return;
    }
    setDeletingJobUuid(job.job_uuid);
    setErrorMessage(null);
    try {
      await apiClient.deleteAdminExportJob(job.job_uuid);
      setJobs((current) => current.filter((item) => item.job_uuid !== job.job_uuid));
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "导出作业删除失败。");
    } finally {
      setDeletingJobUuid(null);
    }
  };

  const jobColumns = useMemo<DataTableColumn<ExportJobView>[]>(
    () => [
      { key: "type", header: "类型", render: (row) => formatExportType(row.export_type) },
      {
        key: "status",
        header: "状态",
        render: (row) => <StatusBadge tone={statusTone(row.status)}>{formatStatusLabel(row.status)}</StatusBadge>,
      },
      { key: "date_range", header: "日期范围", render: (row) => formatJobDateRange(row.filters) },
      { key: "include_test", header: "测试数据", render: (row) => (row.include_test ? "包含" : "不包含") },
      { key: "created", header: "创建", render: (row) => formatDateTime(row.created_at) },
      { key: "completed", header: "完成", render: (row) => formatDateTime(row.completed_at) },
      { key: "message", header: "消息", render: (row) => row.error_message ?? row.progress_message ?? "-" },
      {
        key: "download",
        header: "下载",
        render: (row) =>
          row.status === "succeeded" ? (
            <a className="admin-download-link" href={`/api/admin/export-jobs/${row.job_uuid}/download`}>
              下载
            </a>
          ) : (
            "-"
          ),
      },
      {
        key: "delete",
        header: "删除",
        render: (row) => {
          const isRunning = ["queued", "running"].includes(row.status);
          return (
            <button
              className="admin-icon-button admin-icon-button--danger"
              type="button"
              aria-label="删除作业"
              title={isRunning ? "运行中的作业不能删除" : "删除作业"}
              onClick={() => void deleteJob(row)}
              disabled={isRunning || deletingJobUuid === row.job_uuid}
            >
              <Trash2 size={15} />
            </button>
          );
        },
      },
    ],
    [deletingJobUuid],
  );

  useEffect(() => {
    void load();
  }, [auditStatus]);

  useEffect(() => {
    if (!runningJobs) {
      return undefined;
    }
    const id = window.setInterval(() => {
      void apiClient.listAdminExportJobs().then((payload) => setJobs(payload.items));
    }, 5000);
    return () => window.clearInterval(id);
  }, [runningJobs]);

  return (
    <div className="admin-section-stack">
      <header className="admin-section-header">
        <div>
          <p className="admin-kicker">Export</p>
          <h1>数据导出</h1>
        </div>
        <button className="admin-secondary-button" type="button" onClick={() => void load()} disabled={isLoading}>
          <RefreshCw size={16} />
          <span>{isLoading ? "刷新中" : "刷新"}</span>
        </button>
      </header>

      {errorMessage ? <p className="admin-inline-error">{errorMessage}</p> : null}

      <section className="admin-panel">
        <div className="admin-export-actions">
          <label className="admin-field">
            <span>开始日期</span>
            <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
          </label>
          <label className="admin-field">
            <span>结束日期</span>
            <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
          </label>
          <button
            className="admin-secondary-button"
            type="button"
            onClick={() => {
              setStartDate("");
              setEndDate("");
            }}
            disabled={!startDate && !endDate}
          >
            <RotateCcw size={16} />
            <span>清除日期</span>
          </button>
          <label className="admin-check-row">
            <input
              type="checkbox"
              checked={includeTest}
              onChange={(event) => setIncludeTest(event.target.checked)}
            />
            <span>包含测试通道数据</span>
          </label>
          <button
            className="admin-primary-button"
            type="button"
            onClick={() => void createJob("experiment_data")}
            disabled={isCreating !== null || dateRangeInvalid}
          >
            <Download size={16} />
            <span>{isCreating === "experiment_data" ? "创建中" : "导出全部实验数据"}</span>
          </button>
          <button
            className="admin-secondary-button"
            type="button"
            onClick={() => void createJob("complete_no_external_error_data")}
            disabled={isCreating !== null || dateRangeInvalid}
          >
            <Download size={16} />
            <span>导出完整无外源错误数据</span>
          </button>
          <button
            className="admin-secondary-button"
            type="button"
            onClick={() => void createJob("reimbursement")}
            disabled={isCreating !== null || dateRangeInvalid}
          >
            <Download size={16} />
            <span>导出报销数据</span>
          </button>
        </div>
      </section>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">Export Jobs</p>
            <h2>作业列表</h2>
          </div>
          <StatusBadge tone={runningJobs ? "info" : "neutral"}>
            {runningJobs ? "运行中" : `${formatNumber(jobs.length)} 个作业`}
          </StatusBadge>
        </div>
        <DataTable columns={jobColumns} rows={jobs} getRowKey={(row) => row.job_uuid} />
      </section>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">Clean Data Audit</p>
            <h2>完整数据审核</h2>
          </div>
          <div className="admin-inline-form">
            <select value={auditStatus} onChange={(event) => setAuditStatus(event.target.value)}>
              <option value="">状态：全部</option>
              <option value="eligible">合格</option>
              <option value="review_needed">需复核</option>
              <option value="excluded">已排除</option>
            </select>
            <button className="admin-secondary-button" type="button" onClick={() => void updateAudits()} disabled={isUpdatingAudits}>
              <RefreshCw size={16} />
              <span>{isUpdatingAudits ? "更新中" : "更新"}</span>
            </button>
          </div>
        </div>
        <p className="admin-muted-line">更新时间：{formatDateTime(audits?.last_updated_at)}</p>
        {audits?.summary ? (
          <div className="admin-audit-summary">
            <StatusBadge tone="info">{`已扫描 ${formatNumber(audits.summary.scanned)}`}</StatusBadge>
            <StatusBadge tone="good">{`合格 ${formatNumber(audits.summary.status_counts.eligible ?? 0)}`}</StatusBadge>
            <StatusBadge tone="warning">{`需复核 ${formatNumber(audits.summary.status_counts.review_needed ?? 0)}`}</StatusBadge>
            <StatusBadge tone="neutral">{`已排除 ${formatNumber(audits.summary.status_counts.excluded ?? 0)}`}</StatusBadge>
          </div>
        ) : null}
        <DataTable
          columns={auditColumns}
          rows={audits?.items ?? []}
          getRowKey={(row) => `${row.participant_id}:${row.attempt_id ?? "none"}`}
        />
      </section>
    </div>
  );
}

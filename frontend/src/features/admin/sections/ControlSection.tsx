import { RefreshCw, Save } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiClient } from "../../../api/client";
import type {
  AdminAssignmentControlView,
  AssignmentCellView,
  RecruitmentStatusView,
} from "../../../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { MetricCard } from "../components/MetricCard";
import { StatusBadge } from "../components/StatusBadge";
import { reconcileRecruitmentUpdate } from "../recruitmentState";
import {
  formatAssignmentBatchSelectedCells,
  formatAssignmentBatchScope,
  formatCondition,
  formatErrorType,
  formatNumber,
  formatParticipantType,
  formatSubcondition,
} from "../adminTypes";

interface CellDraft {
  cap: string;
  enabled: boolean;
}

interface AssignmentFilters {
  participantType: string;
  condition: string;
  subcondition: string;
  errorType: string;
  enabled: string;
  cap: string;
  keyword: string;
}

interface ControlSectionProps {
  registerUnsavedNavigationGuard: (guard: (() => Promise<boolean>) | null) => void;
}

const initialFilters: AssignmentFilters = {
  participantType: "",
  condition: "",
  subcondition: "",
  errorType: "",
  enabled: "",
  cap: "",
  keyword: "",
};

function cellKey(cell: AssignmentCellView): string {
  return `${cell.participant_type}:${cell.condition}:${cell.subcondition}:${cell.error_type_id}`;
}

function getAllCells(summary: AdminAssignmentControlView | null): AssignmentCellView[] {
  if (!summary) {
    return [];
  }
  return Object.values(summary.participant_types).flatMap((group) => group.cells);
}

function matchesFilters(cell: AssignmentCellView, filters: AssignmentFilters): boolean {
  if (filters.participantType && cell.participant_type !== filters.participantType) return false;
  if (filters.condition && cell.condition !== filters.condition) return false;
  if (filters.subcondition && cell.subcondition !== filters.subcondition) return false;
  if (filters.errorType && cell.error_type_id !== filters.errorType) return false;
  if (filters.enabled === "enabled" && !cell.enabled) return false;
  if (filters.enabled === "disabled" && cell.enabled) return false;
  if (filters.cap === "reached" && !(cell.cap !== null && cell.count >= cell.cap)) return false;
  if (filters.cap === "uncapped" && cell.cap !== null) return false;
  if (filters.keyword) {
    const haystack = [
      cell.participant_type,
      cell.condition,
      cell.subcondition,
      cell.error_type_id,
    ].join(" ");
    if (!haystack.toLowerCase().includes(filters.keyword.toLowerCase())) {
      return false;
    }
  }
  return true;
}

function parseCapValue(rawCap: string): number | null {
  const normalizedCap = rawCap.trim();
  if (!normalizedCap) {
    return null;
  }
  const parsedCap = Number.parseInt(normalizedCap, 10);
  if (!Number.isInteger(parsedCap) || parsedCap < 0 || String(parsedCap) !== normalizedCap) {
    throw new Error("上限必须是非负整数。");
  }
  return parsedCap;
}

function describeFilters(filters: AssignmentFilters): string {
  const descriptions = [
    filters.participantType ? `被试类型=${formatParticipantType(filters.participantType)}` : null,
    filters.condition ? `条件=${formatCondition(filters.condition)}` : null,
    filters.subcondition ? `子条件=${formatSubcondition(filters.subcondition)}` : null,
    filters.errorType ? `错误类型=${formatErrorType(filters.errorType)}` : null,
    filters.enabled === "enabled" ? "启用状态=已启用" : null,
    filters.enabled === "disabled" ? "启用状态=已停用" : null,
    filters.cap === "reached" ? "上限状态=已达上限" : null,
    filters.cap === "uncapped" ? "上限状态=未设置上限" : null,
    filters.keyword ? `关键词=${filters.keyword}` : null,
  ].filter((description): description is string => Boolean(description));
  return descriptions.length ? descriptions.join("，") : "全部分配单元（显式列表）";
}

async function confirmRenderedBatchPreview(
  message: string,
  selectedCellGroups: string[],
): Promise<boolean> {
  await new Promise<void>((resolve) => {
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve()));
  });
  return window.confirm(
    `${message}\n\n精确分配范围：\n${selectedCellGroups.join("\n")}\n\n确认提交？`,
  );
}

export function ControlSection({ registerUnsavedNavigationGuard }: ControlSectionProps) {
  const [summary, setSummary] = useState<AdminAssignmentControlView | null>(null);
  const [recruitment, setRecruitment] = useState<RecruitmentStatusView | null>(null);
  const [recruitmentOpenDraft, setRecruitmentOpenDraft] = useState<boolean | null>(null);
  const [recruitmentDraftDirty, setRecruitmentDraftDirty] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, CellDraft>>({});
  const [filters, setFilters] = useState<AssignmentFilters>(initialFilters);
  const [batchCap, setBatchCap] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isRecruitmentSaving, setIsRecruitmentSaving] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [batchPreviewMessage, setBatchPreviewMessage] = useState<string | null>(null);
  const [batchPreviewCellGroups, setBatchPreviewCellGroups] = useState<string[]>([]);
  const recruitmentChanged = recruitmentDraftDirty;
  const controlsLocked = isLoading || isSaving || isRecruitmentSaving || recruitment === null;

  const hydrateSummary = (nextSummary: AdminAssignmentControlView) => {
    const nextDrafts: Record<string, CellDraft> = {};
    for (const cell of getAllCells(nextSummary)) {
      nextDrafts[cellKey(cell)] = {
        cap: cell.cap === null ? "" : String(cell.cap),
        enabled: cell.enabled,
      };
    }
    setSummary(nextSummary);
    setDrafts(nextDrafts);
  };

  const hydrateConfirmedRecruitment = (nextRecruitment: RecruitmentStatusView) => {
    setRecruitment(nextRecruitment);
    if (
      !recruitmentDraftDirty ||
      recruitmentOpenDraft === nextRecruitment.accepting_new_participants
    ) {
      setRecruitmentOpenDraft(nextRecruitment.accepting_new_participants);
      setRecruitmentDraftDirty(false);
    }
  };

  const load = async () => {
    setIsLoading(true);
    setErrorMessage(null);
    setRecruitment(null);
    const [summaryResult, recruitmentResult] = await Promise.allSettled([
      apiClient.getAdminAssignmentControl(),
      apiClient.getRecruitmentStatus(),
    ]);
    if (summaryResult.status === "fulfilled") {
      const nextSummary = summaryResult.value;
      hydrateSummary(nextSummary);
    }
    if (recruitmentResult.status === "fulfilled") {
      hydrateConfirmedRecruitment(recruitmentResult.value);
    }
    if (summaryResult.status === "rejected" || recruitmentResult.status === "rejected") {
      setErrorMessage("实验控制加载失败，请稍后重试。");
    }
    setIsLoading(false);
  };

  useEffect(() => {
    void load();
  }, []);

  const saveRecruitment = useCallback(async (): Promise<boolean> => {
    if (recruitment === null || recruitmentOpenDraft === null) {
      return false;
    }
    const requestedOpen = recruitmentOpenDraft;
    setIsRecruitmentSaving(true);
    setErrorMessage(null);
    setRecruitment(null);
    try {
      const result = await reconcileRecruitmentUpdate(requestedOpen, {
        setRecruitment: apiClient.setAdminRecruitment,
        getRecruitmentStatus: apiClient.getRecruitmentStatus,
      });
      setRecruitment(result.status);
      setErrorMessage(result.errorMessage);
      if (result.status?.accepting_new_participants === requestedOpen) {
        setRecruitmentOpenDraft(result.status.accepting_new_participants);
        setRecruitmentDraftDirty(false);
        return true;
      }
      setRecruitmentDraftDirty(true);
      return false;
    } finally {
      setIsRecruitmentSaving(false);
    }
  }, [recruitment, recruitmentOpenDraft]);

  const cells = useMemo(() => getAllCells(summary), [summary]);
  const filteredCells = useMemo(
    () => cells.filter((cell) => matchesFilters(cell, filters)),
    [cells, filters],
  );
  const cappedCount = cells.filter((cell) => cell.cap !== null && cell.count >= cell.cap).length;
  const dirtyCells = useMemo(
    () =>
      cells.filter((cell) => {
        const draft = drafts[cellKey(cell)];
        if (!draft) {
          return false;
        }
        const currentCap = cell.cap === null ? "" : String(cell.cap);
        return draft.enabled !== cell.enabled || draft.cap.trim() !== currentCap;
      }),
    [cells, drafts],
  );
  const hasUnsavedChanges = recruitmentChanged || dirtyCells.length > 0;

  const saveAllChanges = useCallback(async (): Promise<boolean> => {
    if (!summary) {
      return true;
    }

    let parsedDirtyCells: Array<{ cell: AssignmentCellView; cap: number | null; enabled: boolean }> = [];
    try {
      parsedDirtyCells = dirtyCells.map((cell) => {
        const draft = drafts[cellKey(cell)];
        return {
          cell,
          cap: parseCapValue(draft?.cap ?? ""),
          enabled: draft?.enabled ?? cell.enabled,
        };
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "分配上限格式不正确。");
      return false;
    }

    setIsSaving(true);
    setErrorMessage(null);
    setBatchPreviewMessage(null);
    setBatchPreviewCellGroups([]);
    try {
      if (parsedDirtyCells.length) {
        const request = {
          scope: {
            cells: parsedDirtyCells.map(({ cell }) => ({
              participant_type: cell.participant_type,
              condition: cell.condition,
              subcondition: cell.subcondition,
              error_type_id: cell.error_type_id,
            })),
          },
          changes: {},
          cell_updates: parsedDirtyCells.map(({ cell, cap, enabled }) => ({
            participant_type: cell.participant_type,
            condition: cell.condition,
            subcondition: cell.subcondition,
            error_type_id: cell.error_type_id,
            cap,
            enabled,
          })),
        };
        const preview = await apiClient.previewAdminAssignmentBatch(request);
        const previewMessage = [
          `保存全部修改将影响 ${formatNumber(preview.affected_count)} 个分配单元。`,
          `提交范围：${formatAssignmentBatchScope(preview.scope)}。`,
        ].join(" ");
        const selectedCellGroups = formatAssignmentBatchSelectedCells(preview.scope);
        setBatchPreviewMessage(previewMessage);
        setBatchPreviewCellGroups(selectedCellGroups);
        if (!await confirmRenderedBatchPreview(previewMessage, selectedCellGroups)) {
          return false;
        }
        const result = await apiClient.applyAdminAssignmentBatch({
          ...request,
          scope_version: preview.scope_version,
        });
        hydrateSummary(result.assignment_control);
      }
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setErrorMessage("保存范围已变化或预览已过期，请刷新后重新预览并确认。");
      } else {
        setErrorMessage(error instanceof Error ? error.message : "分配条件保存失败。");
      }
      return false;
    } finally {
      setIsSaving(false);
    }
  }, [
    dirtyCells,
    drafts,
    summary,
  ]);

  const savePendingChanges = useCallback(async (): Promise<boolean> => {
    if (recruitmentChanged && !await saveRecruitment()) return false;
    if (dirtyCells.length > 0 && !await saveAllChanges()) return false;
    return true;
  }, [dirtyCells.length, recruitmentChanged, saveAllChanges, saveRecruitment]);

  const saveCell = async (cell: AssignmentCellView) => {
    const draft = drafts[cellKey(cell)];
    let cap: number | null = null;
    try {
      cap = parseCapValue(draft?.cap ?? "");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "分配上限格式不正确。");
      return;
    }
    setIsSaving(true);
    setErrorMessage(null);
    try {
      hydrateSummary(
        await apiClient.updateAdminAssignmentControl({
          operation: "cell",
          participant_type: cell.participant_type,
          condition: cell.condition,
          subcondition: cell.subcondition,
          error_type_id: cell.error_type_id,
          cap,
          enabled: draft?.enabled ?? cell.enabled,
        }),
      );
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "分配单元保存失败。");
    } finally {
      setIsSaving(false);
    }
  };

  const applyBatch = async (mode: "enable" | "disable" | "cap") => {
    if (!filteredCells.length || hasUnsavedChanges) {
      return;
    }
    let parsedBatchCap: number | null = null;
    if (mode === "cap") {
      try {
        parsedBatchCap = parseCapValue(batchCap);
      } catch (error) {
        setErrorMessage(error instanceof Error ? error.message : "批量上限格式不正确。");
        return;
      }
    }
    setIsSaving(true);
    setErrorMessage(null);
    setBatchPreviewMessage(null);
    setBatchPreviewCellGroups([]);
    try {
      const changes =
        mode === "cap"
          ? { cap: parsedBatchCap }
          : { enabled: mode === "enable" };
      const request = {
        scope: {
          cells: filteredCells.map((cell) => ({
            participant_type: cell.participant_type,
            condition: cell.condition,
            subcondition: cell.subcondition,
            error_type_id: cell.error_type_id,
          })),
        },
        changes,
      };
      const preview = await apiClient.previewAdminAssignmentBatch(request);
      const actionLabel =
        mode === "enable"
          ? "批量启用"
          : mode === "disable"
            ? "批量禁用"
            : `批量设置上限为 ${parsedBatchCap === null ? "不限" : parsedBatchCap}`;
      const previewMessage = [
        `${actionLabel}将影响 ${formatNumber(preview.affected_count)} 个分配单元。`,
        `提交范围：${formatAssignmentBatchScope(preview.scope)}。`,
        `当前筛选：${describeFilters(filters)}。`,
      ].join(" ");
      const selectedCellGroups = formatAssignmentBatchSelectedCells(preview.scope);
      setBatchPreviewMessage(previewMessage);
      setBatchPreviewCellGroups(selectedCellGroups);
      if (!await confirmRenderedBatchPreview(previewMessage, selectedCellGroups)) {
        return;
      }
      const result = await apiClient.applyAdminAssignmentBatch({
        ...request,
        scope_version: preview.scope_version,
      });
      hydrateSummary(result.assignment_control);
      setBatchPreviewMessage(
        `已更新 ${formatNumber(result.result.updated_cells)} 个分配单元。`,
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setErrorMessage("批量范围已变化或预览已过期，请刷新后重新预览并确认。");
      } else {
        setErrorMessage(error instanceof Error ? error.message : "批量操作失败。");
      }
    } finally {
      setIsSaving(false);
    }
  };

  useEffect(() => {
    if (isSaving || isRecruitmentSaving) {
      registerUnsavedNavigationGuard(async () => false);
      return () => registerUnsavedNavigationGuard(null);
    }
    if (!hasUnsavedChanges) {
      registerUnsavedNavigationGuard(null);
      return () => registerUnsavedNavigationGuard(null);
    }

    registerUnsavedNavigationGuard(async () => {
      const shouldSave = window.confirm(
        "实验控制有未保存修改。选择“确定”保存并离开，选择“取消”留在当前页面继续编辑。",
      );
      if (!shouldSave) {
        return false;
      }
      return savePendingChanges();
    });
    return () => registerUnsavedNavigationGuard(null);
  }, [
    hasUnsavedChanges,
    isRecruitmentSaving,
    isSaving,
    registerUnsavedNavigationGuard,
    savePendingChanges,
  ]);

  useEffect(() => {
    if (!hasUnsavedChanges) {
      return undefined;
    }
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [hasUnsavedChanges]);

  const columns: DataTableColumn<AssignmentCellView>[] = [
    { key: "participant_type", header: "被试类型", render: (cell) => formatParticipantType(cell.participant_type) },
    { key: "condition", header: "条件", render: (cell) => formatCondition(cell.condition) },
    { key: "subcondition", header: "子条件", render: (cell) => formatSubcondition(cell.subcondition) },
    { key: "error", header: "错误类型", render: (cell) => formatErrorType(cell.error_type_id) },
    { key: "clean_count", header: "完整无外源错误", render: (cell) => formatNumber(cell.complete_no_external_error_count) },
    { key: "active_count", header: "已分配正在实验", render: (cell) => formatNumber(cell.active_assignment_count) },
    { key: "count", header: "计入上限", render: (cell) => formatNumber(cell.count) },
    { key: "remaining", header: "剩余分配", render: (cell) => (cell.remaining === null ? "不限" : formatNumber(cell.remaining)) },
    {
      key: "enabled",
      header: "是否启用",
      render: (cell) => {
        const key = cellKey(cell);
        return (
          <input
            type="checkbox"
            disabled={controlsLocked}
            aria-label={`${formatParticipantType(cell.participant_type)} ${formatCondition(cell.condition)} ${formatSubcondition(cell.subcondition)} ${formatErrorType(cell.error_type_id)} 是否启用`}
            checked={drafts[key]?.enabled ?? cell.enabled}
            onChange={(event) =>
              setDrafts((current) => ({
                ...current,
                [key]: {
                  cap: current[key]?.cap ?? (cell.cap === null ? "" : String(cell.cap)),
                  enabled: event.target.checked,
                },
              }))
            }
          />
        );
      },
    },
    {
      key: "cap",
      header: "上限",
      render: (cell) => {
        const key = cellKey(cell);
        return (
          <input
            className="admin-table-input"
            disabled={controlsLocked}
            value={drafts[key]?.cap ?? ""}
            inputMode="numeric"
            onChange={(event) =>
              setDrafts((current) => ({
                ...current,
                [key]: {
                  cap: event.target.value,
                  enabled: current[key]?.enabled ?? cell.enabled,
                },
              }))
            }
          />
        );
      },
    },
    {
      key: "save",
      header: "保存",
      render: (cell) => (
        <button className="admin-icon-button" type="button" onClick={() => void saveCell(cell)} disabled={controlsLocked}>
          <Save size={15} />
        </button>
      ),
    },
  ];

  return (
    <div className="admin-section-stack">
      <header className="admin-section-header">
        <div>
          <p className="admin-kicker">Experiment Control</p>
          <h1>实验控制</h1>
        </div>
        <button
          className="admin-secondary-button"
          type="button"
          onClick={() => void load()}
          disabled={isLoading || isSaving || isRecruitmentSaving}
        >
          <RefreshCw size={16} />
          <span>{isLoading ? "刷新中" : "刷新"}</span>
        </button>
      </header>

      {errorMessage ? <p className="admin-inline-error">{errorMessage}</p> : null}
      {batchPreviewMessage ? <p className="admin-panel-note">{batchPreviewMessage}</p> : null}
      {batchPreviewCellGroups.length ? (
        <section className="admin-panel" aria-label="批量操作精确范围">
          <div className="admin-panel-heading">
            <div>
              <p className="admin-kicker">Confirmed Scope</p>
              <h2>批量操作精确范围</h2>
            </div>
            <StatusBadge tone="info">
              {`${formatNumber(batchPreviewCellGroups.length)} 组`}
            </StatusBadge>
          </div>
          <ul className="admin-panel-note">
            {batchPreviewCellGroups.map((group) => <li key={group}>{group}</li>)}
          </ul>
        </section>
      ) : null}

      <div className="admin-metric-grid">
        <MetricCard label="分配单元" value={formatNumber(cells.length)} />
        <MetricCard label="当前筛选" value={formatNumber(filteredCells.length)} />
        <MetricCard label="达到上限" value={formatNumber(cappedCount)} tone={cappedCount ? "warning" : "default"} />
        <MetricCard label="测试通道" value={summary?.current_flags.test_channel_enabled ? "已启用" : "已停用"} detail="管理员鉴权" />
      </div>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">Recruitment</p>
            <h2>正式招募</h2>
            <p className="admin-panel-note">
              关闭后仅阻止新被试注册，已有被试仍可继续或恢复实验。
            </p>
          </div>
          <button
            className="admin-primary-button"
            type="button"
            onClick={() => void saveRecruitment()}
            disabled={controlsLocked || !recruitmentChanged}
          >
            <Save size={16} />
            <span>{isRecruitmentSaving ? "保存中" : "保存"}</span>
          </button>
          {recruitment === null ? (
            <StatusBadge tone={isLoading ? "info" : "neutral"}>
              {isLoading ? "加载中" : "状态不可用"}
            </StatusBadge>
          ) : recruitment.accepting_new_participants ? (
            <StatusBadge tone="good">开放</StatusBadge>
          ) : (
            <StatusBadge tone="warning">暂停</StatusBadge>
          )}
        </div>
        <div className="admin-toggle-grid">
          <label className="admin-toggle-row">
            <input
              type="checkbox"
              checked={recruitmentOpenDraft ?? false}
              disabled={controlsLocked || recruitment === null}
              onChange={(event) => {
                const nextOpen = event.target.checked;
                setRecruitmentOpenDraft(nextOpen);
                setRecruitmentDraftDirty(
                  recruitment === null ||
                    nextOpen !== recruitment.accepting_new_participants,
                );
              }}
            />
            <span>允许新被试注册</span>
          </label>
        </div>
      </section>

      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <p className="admin-kicker">Assignment Units</p>
            <h2>分配控制</h2>
          </div>
          <button
            className="admin-primary-button"
            type="button"
            onClick={() => void saveAllChanges()}
            disabled={controlsLocked || dirtyCells.length === 0}
          >
            <Save size={16} />
            <span>{isSaving ? "保存中" : "保存全部修改"}</span>
          </button>
        </div>
        <div className="admin-filter-grid">
          <select disabled={controlsLocked} value={filters.participantType} onChange={(event) => setFilters((current) => ({ ...current, participantType: event.target.value }))}>
            <option value="">类型：全部</option>
            <option value="short">短程</option>
            <option value="long">长程</option>
          </select>
          <select disabled={controlsLocked} value={filters.condition} onChange={(event) => setFilters((current) => ({ ...current, condition: event.target.value }))}>
            <option value="">条件：全部</option>
            <option value="human">人类来源</option>
            <option value="tool">工具来源</option>
          </select>
          <select disabled={controlsLocked} value={filters.subcondition} onChange={(event) => setFilters((current) => ({ ...current, subcondition: event.target.value }))}>
            <option value="">子条件：全部</option>
            <option value="qa">问答</option>
            <option value="planning">规划</option>
            <option value="chat">聊天</option>
            <option value="decision">决策</option>
            <option value="execution">执行</option>
          </select>
          <select disabled={controlsLocked} value={filters.enabled} onChange={(event) => setFilters((current) => ({ ...current, enabled: event.target.value }))}>
            <option value="">启用状态：全部</option>
            <option value="enabled">已启用</option>
            <option value="disabled">已停用</option>
          </select>
          <select disabled={controlsLocked} value={filters.cap} onChange={(event) => setFilters((current) => ({ ...current, cap: event.target.value }))}>
            <option value="">上限状态：全部</option>
            <option value="reached">已达上限</option>
            <option value="uncapped">未设置上限</option>
          </select>
          <input
            disabled={controlsLocked}
            value={filters.keyword}
            placeholder="搜索条件或错误类型"
            onChange={(event) => setFilters((current) => ({ ...current, keyword: event.target.value }))}
          />
        </div>
        <div className="admin-batch-row">
          <button className="admin-secondary-button" type="button" onClick={() => void applyBatch("enable")} disabled={controlsLocked || !filteredCells.length || hasUnsavedChanges}>
            批量启用
          </button>
          <button className="admin-secondary-button" type="button" onClick={() => void applyBatch("disable")} disabled={controlsLocked || !filteredCells.length || hasUnsavedChanges}>
            批量禁用
          </button>
          <input disabled={controlsLocked} value={batchCap} placeholder="批量上限" onChange={(event) => setBatchCap(event.target.value)} />
          <button className="admin-secondary-button" type="button" onClick={() => void applyBatch("cap")} disabled={controlsLocked || !filteredCells.length || hasUnsavedChanges}>
            批量设置上限
          </button>
        </div>
        <DataTable columns={columns} rows={filteredCells} getRowKey={(cell) => cellKey(cell)} />
      </section>
    </div>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2 } from "lucide-react";

import { apiClient, ApiError } from "../../api/client";
import type { PretestResponseView, PretestSubmissionRequest } from "../../api/types";
import { interfaceCopy } from "../../experiment/interfaceCopy";
import {
  frequencyOptions,
  getRequiredPretestItemIds,
  likertValues,
  pretestSections,
  type PretestItem,
} from "./pretestConfig";

interface PretestFormProps {
  initialPayload?: Record<string, unknown>;
  onEnterExperiment: (response: PretestResponseView) => void | Promise<void>;
  enterExperimentError?: string | null;
}

type PretestStep = "intro" | "demographics" | "scales" | "save";

interface PretestDraft {
  demographics: {
    birthDate?: string;
    gender?: string;
    idNumber?: string;
  };
  scales: Record<string, string | number>;
  slider_touch_state: Record<string, boolean>;
  page_progress: Record<string, unknown>;
  client_timestamp: string;
}

const STEP_ORDER: PretestStep[] = ["intro", "demographics", "scales", "save"];
const SCALE_ITEMS = pretestSections.flatMap((section) => section.items);
const SLIDER_ITEMS = SCALE_ITEMS.filter((item) => item.type === "slider");
const AUTOSAVE_DEBOUNCE_MS = 800;

function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function readScaleRecord(value: unknown): Record<string, string | number> {
  const record = readRecord(value);
  return Object.fromEntries(
    Object.entries(record).filter(
      ([, entryValue]) => typeof entryValue === "string" || typeof entryValue === "number",
    ),
  ) as Record<string, string | number>;
}

function readSliderTouchStateRecord(value: unknown): Record<string, boolean> {
  const record = readRecord(value);
  return Object.fromEntries(
    Object.entries(record).filter(([, entryValue]) => typeof entryValue === "boolean"),
  ) as Record<string, boolean>;
}

function buildInitialDraft(initialPayload?: Record<string, unknown>): PretestDraft {
  const demographics = readRecord(initialPayload?.demographics);
  const scales = readScaleRecord(initialPayload?.scales);
  const sliderTouchState = readSliderTouchStateRecord(initialPayload?.slider_touch_state);
  const pageProgress = readRecord(initialPayload?.page_progress);

  return {
    demographics: {
      birthDate: typeof demographics.birthDate === "string" ? demographics.birthDate : "",
      gender: typeof demographics.gender === "string" ? demographics.gender : "",
      idNumber: typeof demographics.idNumber === "string" ? demographics.idNumber : "",
    },
    scales,
    slider_touch_state: sliderTouchState,
    page_progress: pageProgress,
    client_timestamp:
      typeof initialPayload?.client_timestamp === "string"
        ? initialPayload.client_timestamp
        : new Date().toISOString(),
  };
}

function buildRequestPayload(draft: PretestDraft, step: PretestStep): PretestSubmissionRequest {
  const completed_steps = STEP_ORDER.filter((currentStep) => STEP_ORDER.indexOf(currentStep) < STEP_ORDER.indexOf(step));
  const demographics = Object.fromEntries(
    Object.entries(draft.demographics).filter(
      ([, value]) => typeof value === "string" && value.trim().length > 0,
    ),
  );
  return {
    demographics,
    scales: draft.scales,
    slider_touch_state: draft.slider_touch_state,
    page_progress: {
      ...draft.page_progress,
      section: step,
      current_step: step,
      completed_steps,
    },
    client_timestamp: new Date().toISOString(),
  };
}

function payloadFingerprint(payload: PretestSubmissionRequest): string {
  return JSON.stringify({
    demographics: payload.demographics,
    scales: payload.scales,
    slider_touch_state: payload.slider_touch_state,
    page_progress: payload.page_progress,
  });
}

function getRestoredStep(payload: Record<string, unknown>): PretestStep {
  const pageProgress = readRecord(payload.page_progress);
  const candidate = pageProgress.current_step ?? pageProgress.section;
  return STEP_ORDER.includes(candidate as PretestStep) ? (candidate as PretestStep) : "intro";
}

function hasDraftContent(draft: PretestDraft, step: PretestStep): boolean {
  return (
    step !== "intro" ||
    Object.values(draft.demographics).some((value) => Boolean(value?.trim())) ||
    Object.keys(draft.scales).length > 0
  );
}

function isValidDraftForAutosave(draft: PretestDraft): boolean {
  const { birthDate = "", gender = "", idNumber = "" } = draft.demographics;
  if (birthDate) {
    const parsedBirthDate = new Date(`${birthDate}T00:00:00`);
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(birthDate) ||
      Number.isNaN(parsedBirthDate.getTime()) ||
      parsedBirthDate > new Date()
    ) {
      return false;
    }
  }
  if (gender && !["男", "女"].includes(gender)) {
    return false;
  }
  if (idNumber && ![9, 18].includes(idNumber.trim().length)) {
    return false;
  }

  return SCALE_ITEMS.every((item) => {
    const value = draft.scales[item.id];
    if (value === undefined || value === "") {
      return draft.slider_touch_state[item.id] !== true;
    }
    if (item.type === "likert") {
      return typeof value === "number" && likertValues.includes(value as 1 | 2 | 3 | 4 | 5);
    }
    if (item.type === "frequency") {
      return frequencyOptions.some((option) => option.value === value);
    }
    return (
      typeof value === "number" &&
      value >= item.min &&
      value <= item.max &&
      draft.slider_touch_state[item.id] === true
    );
  });
}

function getItemValue(draft: PretestDraft, itemId: string): string {
  const value = draft.scales[itemId];
  if (typeof value === "number") {
    return String(value);
  }
  return typeof value === "string" ? value : "";
}

function parseSliderValue(value: string): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 50;
  }
  return Math.max(1, Math.min(100, parsed));
}

function hasSliderTouchFieldErrors(fieldErrors: Record<string, string>) {
  return Object.keys(fieldErrors).some((field) => field.startsWith("slider_touch_state."));
}

function normalizePretestFieldErrors(fieldErrors: Record<string, string>) {
  return Object.fromEntries(
    Object.entries(fieldErrors).map(([field, message]) => [
      field,
      field.startsWith("slider_touch_state.")
        ? interfaceCopy.pretest.sliderTouchField
        : message,
    ]),
  );
}

function formatEnterExperimentError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "进入正式实验失败。";
}

export function PretestForm({
  initialPayload,
  onEnterExperiment,
  enterExperimentError,
}: PretestFormProps) {
  const [step, setStep] = useState<PretestStep>("intro");
  const [draft, setDraft] = useState<PretestDraft>(() => buildInitialDraft(initialPayload));
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [autosaveMessage, setAutosaveMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [isRestoring, setIsRestoring] = useState(true);
  const [isAutosaving, setIsAutosaving] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [isEnteringExperiment, setIsEnteringExperiment] = useState(false);
  const [finalResponse, setFinalResponse] = useState<PretestResponseView | null>(null);
  const draftRef = useRef(draft);
  const stepRef = useRef(step);
  const autosaveTimerRef = useRef<number | null>(null);
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const nextSaveVersionRef = useRef(0);
  const latestAcknowledgedVersionRef = useRef(0);
  const acknowledgedPayloadRef = useRef<string | null>(null);
  const pendingFinalPayloadRef = useRef<PretestSubmissionRequest | null>(null);

  if (import.meta.env.DEV) {
    const ids = getRequiredPretestItemIds();
    console.assert(ids.includes("q1") && ids.includes("q49"), "pretest q1-q49 must be present");
    console.assert(
      ids.includes("confidence_q27") && ids.includes("confidence_q46"),
      "pretest confidence sliders must be present",
    );
  }

  const demographicMissing = useMemo(() => {
    const missing: string[] = [];
    if (!draft.demographics.birthDate) missing.push("birthDate");
    if (!draft.demographics.gender) missing.push("gender");
    if (!draft.demographics.idNumber) missing.push("idNumber");
    return missing;
  }, [draft.demographics]);

  const scaleMissing = useMemo(
    () =>
      SCALE_ITEMS.filter((item) => {
        const value = draft.scales[item.id];
        return value === "" || value === undefined || value === null;
      }).map((item) => item.id),
    [draft.scales],
  );

  const missingSliderIds = useMemo(
    () =>
      SLIDER_ITEMS.map((item) => item.id).filter((id) => draft.slider_touch_state[id] !== true),
    [draft.slider_touch_state],
  );
  const displayedErrorMessage = errorMessage ?? enterExperimentError;

  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);

  useEffect(() => {
    stepRef.current = step;
  }, [step]);

  useEffect(() => {
    let cancelled = false;

    const restoreDraft = async () => {
      try {
        const response = await apiClient.getCurrentPretest();
        if (cancelled || !response) {
          return;
        }
        if (response.status === "final") {
          setFinalResponse(response);
          setSaveMessage(interfaceCopy.pretest.saveSuccess);
          setStep("save");
          stepRef.current = "save";
          return;
        }

        const restoredDraft = buildInitialDraft(response.payload);
        const restoredStep = getRestoredStep(response.payload);
        draftRef.current = restoredDraft;
        stepRef.current = restoredStep;
        acknowledgedPayloadRef.current = payloadFingerprint(
          buildRequestPayload(restoredDraft, restoredStep),
        );
        setDraft(restoredDraft);
        setStep(restoredStep);
        setAutosaveMessage("已恢复上次保存的问卷。");
      } catch (error) {
        if (!cancelled) {
          setErrorMessage(error instanceof ApiError ? error.detail : "无法恢复已保存的前测问卷。");
        }
      } finally {
        if (!cancelled) {
          setIsRestoring(false);
        }
      }
    };

    void restoreDraft();
    return () => {
      cancelled = true;
    };
  }, []);

  const saveDraftPayload = useCallback(
    async (payload: PretestSubmissionRequest, saveVersion: number) => {
      setIsAutosaving(true);
      setAutosaveMessage("正在自动保存…");
      try {
        await apiClient.savePretestDraft(payload);
        if (saveVersion >= latestAcknowledgedVersionRef.current) {
          latestAcknowledgedVersionRef.current = saveVersion;
          acknowledgedPayloadRef.current = payloadFingerprint(payload);
        }
        if (saveVersion === nextSaveVersionRef.current) {
          setAutosaveMessage("已自动保存。");
        }
      } catch (error) {
        if (saveVersion === nextSaveVersionRef.current) {
          setAutosaveMessage("自动保存失败，当前填写内容仍保留在本页，请稍后重试。");
          if (error instanceof ApiError && error.fieldErrors) {
            setFieldErrors(error.fieldErrors);
          }
        }
        throw error;
      } finally {
        if (saveVersion === nextSaveVersionRef.current) {
          setIsAutosaving(false);
        }
      }
    },
    [],
  );

  const runDraftSave = useCallback(
    (payload: PretestSubmissionRequest) => {
      const fingerprint = payloadFingerprint(payload);
      const saveVersion = nextSaveVersionRef.current + 1;
      nextSaveVersionRef.current = saveVersion;
      const previousSave = saveQueueRef.current;
      const queuedSave = previousSave
        .catch(() => undefined)
        .then(async () => {
          if (fingerprint === acknowledgedPayloadRef.current) {
            if (saveVersion === nextSaveVersionRef.current) {
              setIsAutosaving(false);
              setAutosaveMessage("已自动保存。");
            }
            return;
          }
          await saveDraftPayload(payload, saveVersion);
        });
      saveQueueRef.current = queuedSave;
      return queuedSave;
    },
    [saveDraftPayload],
  );

  useEffect(() => {
    return () => {
      if (autosaveTimerRef.current !== null) {
        window.clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (isRestoring || finalResponse || !hasDraftContent(draft, step)) {
      return;
    }
    if (!isValidDraftForAutosave(draft)) {
      return;
    }

    const payload = buildRequestPayload(draft, step);
    if (payloadFingerprint(payload) === acknowledgedPayloadRef.current) {
      return;
    }
    if (autosaveTimerRef.current !== null) {
      window.clearTimeout(autosaveTimerRef.current);
    }
    autosaveTimerRef.current = window.setTimeout(() => {
      autosaveTimerRef.current = null;
      void runDraftSave(payload).catch(() => undefined);
    }, AUTOSAVE_DEBOUNCE_MS);

    return () => {
      if (autosaveTimerRef.current !== null) {
        window.clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
    };
  }, [draft, finalResponse, isRestoring, runDraftSave, step]);

  const flushPendingDraft = useCallback(async () => {
    if (autosaveTimerRef.current !== null) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    await saveQueueRef.current.catch(() => undefined);

    const currentDraft = draftRef.current;
    const currentStep = stepRef.current;
    if (!hasDraftContent(currentDraft, currentStep) || !isValidDraftForAutosave(currentDraft)) {
      return;
    }
    const payload = buildRequestPayload(currentDraft, currentStep);
    await runDraftSave(payload);
  }, [runDraftSave]);

  const clearEditedFieldError = (field: string) => {
    pendingFinalPayloadRef.current = null;
    setErrorMessage(null);
    setAutosaveMessage(null);
    setFieldErrors((current) => {
      if (!(field in current)) {
        return current;
      }
      const next = { ...current };
      delete next[field];
      return next;
    });
  };

  const updateDemographic = (key: keyof PretestDraft["demographics"], value: string) => {
    clearEditedFieldError(`demographics.${key}`);
    setDraft((current) => ({
      ...current,
      demographics: { ...current.demographics, [key]: value },
    }));
  };

  const updateScale = (id: string, value: string | number) => {
    clearEditedFieldError(`scales.${id}`);
    setDraft((current) => ({
      ...current,
      scales: { ...current.scales, [id]: value },
    }));
  };

  const updateSlider = (id: string, value: number) => {
    clearEditedFieldError(`scales.${id}`);
    clearEditedFieldError(`slider_touch_state.${id}`);
    setDraft((current) => ({
      ...current,
      scales: { ...current.scales, [id]: value },
      slider_touch_state: { ...current.slider_touch_state, [id]: true },
    }));
  };

  const validateDemographics = () => {
    const nextFieldErrors: Record<string, string> = {};
    if (demographicMissing.length > 0) {
      demographicMissing.forEach((key) => {
        nextFieldErrors[`demographics.${key}`] = "此项为必填项。";
      });
      setFieldErrors((current) => ({ ...current, ...nextFieldErrors }));
      setErrorMessage(interfaceCopy.pretest.missing);
      return false;
    }
    const idNumber = draft.demographics.idNumber?.trim() ?? "";
    if (idNumber.length !== 18 && idNumber.length !== 9) {
      setFieldErrors((current) => ({
        ...current,
        "demographics.idNumber": interfaceCopy.pretest.invalidId,
      }));
      setErrorMessage(interfaceCopy.pretest.invalidId);
      return false;
    }
    return true;
  };

  const submitFinal = async () => {
    if (!validateDemographics()) {
      setStep("demographics");
      return;
    }

    const firstMissingId = scaleMissing[0] ?? missingSliderIds[0];
    if (firstMissingId) {
      setFieldErrors((current) => ({
        ...current,
        ...Object.fromEntries(scaleMissing.map((id) => [`scales.${id}`, "此项为必填项。"])),
        ...Object.fromEntries(
          missingSliderIds.map((id) => [
            `slider_touch_state.${id}`,
            interfaceCopy.pretest.sliderTouchField,
          ]),
        ),
      }));
      setStep("scales");
      stepRef.current = "scales";
      setErrorMessage(
        scaleMissing.length > 0
          ? interfaceCopy.pretest.missing
          : interfaceCopy.pretest.sliderTouchMissing,
      );
      document
        .querySelector(`[data-pretest-item-id="${firstMissingId}"]`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }

    setIsSaving(true);
    setErrorMessage(null);
    try {
      await flushPendingDraft();
      const payload =
        pendingFinalPayloadRef.current ?? buildRequestPayload(draftRef.current, "save");
      pendingFinalPayloadRef.current = payload;
      const response = await apiClient.submitPretestFinal(payload);
      setFinalResponse(response);
      setFieldErrors({});
      setSaveMessage(interfaceCopy.pretest.saveSuccess);
      setStep("save");
      stepRef.current = "save";
    } catch (error) {
      if (error instanceof ApiError && error.fieldErrors) {
        setFieldErrors(normalizePretestFieldErrors(error.fieldErrors));
      }
      setErrorMessage(
        error instanceof ApiError && error.fieldErrors && hasSliderTouchFieldErrors(error.fieldErrors)
          ? interfaceCopy.pretest.sliderTouchMissing
          : error instanceof ApiError
            ? error.detail
            : "前测提交失败。",
      );
    } finally {
      setIsSaving(false);
    }
  };

  const goNext = () => {
    if (step === "intro") {
      setStep("demographics");
      stepRef.current = "demographics";
      return;
    }
    if (step === "demographics") {
      if (!validateDemographics()) {
        return;
      }
      setErrorMessage(null);
      setStep("scales");
      stepRef.current = "scales";
      return;
    }
    if (step === "scales") {
      void submitFinal();
    }
  };

  const enterExperiment = async () => {
    if (!finalResponse || isEnteringExperiment) {
      return;
    }

    setIsEnteringExperiment(true);
    setErrorMessage(null);
    try {
      await onEnterExperiment(finalResponse);
    } catch (error) {
      setErrorMessage(formatEnterExperimentError(error));
      setIsEnteringExperiment(false);
    }
  };

  const renderFieldError = (field: string) =>
    fieldErrors[field] ? (
      <p className="status-inline status-inline--error" role="alert">
        {fieldErrors[field]}
      </p>
    ) : null;

  const renderScaleItem = (item: PretestItem) => {
    const label = item.no ? `${item.no}. ${item.text}` : item.text;
    const value = getItemValue(draft, item.id);

    if (item.type === "likert") {
      return (
        <div className="pretest-question" key={item.id} data-pretest-item-id={item.id}>
          <div className="pretest-question-title">{label}</div>
          <div className="likert-options" role="radiogroup" aria-label={label}>
            {likertValues.map((likertValue) => (
              <label className="likert-option" key={likertValue}>
                <input
                  type="radio"
                  name={`pretest_${item.id}`}
                  value={likertValue}
                  checked={String(value) === String(likertValue)}
                  onChange={() => updateScale(item.id, likertValue)}
                />
                <span>{likertValue}</span>
              </label>
            ))}
          </div>
          {renderFieldError(`scales.${item.id}`)}
        </div>
      );
    }

    if (item.type === "frequency") {
      return (
        <div className="pretest-question" key={item.id} data-pretest-item-id={item.id}>
          <div className="pretest-question-title">{label}</div>
          <div className="frequency-options" role="radiogroup" aria-label={label}>
            {frequencyOptions.map((option) => (
              <label className="frequency-option" key={option.value}>
                <input
                  type="radio"
                  name={`pretest_${item.id}`}
                  value={option.value}
                  checked={String(value) === String(option.value)}
                  onChange={() => updateScale(item.id, option.value)}
                />
                <span>
                  {option.value}. {option.label}
                </span>
              </label>
            ))}
          </div>
          {renderFieldError(`scales.${item.id}`)}
        </div>
      );
    }

    const sliderValue = parseSliderValue(value || "50");
    const touched = draft.slider_touch_state[item.id] === true;
    const confirmSliderValue = () => updateSlider(item.id, sliderValue);

    return (
      <div className="pretest-question" key={item.id} data-pretest-item-id={item.id}>
        <div className="pretest-question-title">
          {label}
          {item.note ? <small style={{ display: "block", marginTop: 4, whiteSpace: "pre-wrap" }}>{item.note}</small> : null}
        </div>
        <input
          type="range"
          min={item.min}
          max={item.max}
          value={sliderValue}
          onChange={(event) => updateSlider(item.id, Number(event.target.value))}
          onPointerDown={confirmSliderValue}
          onKeyDown={(event) => {
            if (
              [
                "ArrowLeft",
                "ArrowRight",
                "ArrowUp",
                "ArrowDown",
                "Home",
                "End",
                "PageUp",
                "PageDown",
              ].includes(event.key)
            ) {
              confirmSliderValue();
            }
          }}
        />
        <p className="muted">
          当前值：{sliderValue}
          {touched ? "，已调整" : `，${interfaceCopy.pretest.sliderUntouchedHint}`}
        </p>
        {renderFieldError(`scales.${item.id}`)}
        {renderFieldError(`slider_touch_state.${item.id}`)}
      </div>
    );
  };

  return (
    <form
      className={`flow-card pretest-card${step === "scales" ? " flow-card--wide pretest-card--scales" : ""}`}
      onSubmit={(event) => event.preventDefault()}
    >
      <div className="flow-logo" aria-hidden="true">
        <CheckCircle2 size={34} />
      </div>
      <h1 className="flow-title">{interfaceCopy.pretest.title}</h1>

      {isRestoring ? <p className="status-inline">正在恢复已保存的问卷…</p> : null}

      {!isRestoring && step === "intro" ? (
        <p className="pretest-text">{interfaceCopy.pretest.intro}</p>
      ) : null}

      {!isRestoring && step === "demographics" ? (
        <div className="pretest-form-stack">
          <label className="field">
            <span>{interfaceCopy.pretest.birthDate}</span>
            <input
              type="date"
              value={draft.demographics.birthDate ?? ""}
              onChange={(event) => updateDemographic("birthDate", event.target.value)}
            />
            {renderFieldError("demographics.birthDate")}
          </label>
          <fieldset className="field-set pretest-field-set">
            <legend>{interfaceCopy.pretest.gender}</legend>
            <div className="pretest-radio-group">
              {[interfaceCopy.pretest.male, interfaceCopy.pretest.female].map((gender) => (
                <label key={gender}>
                  <input
                    type="radio"
                    name="pretestGender"
                    value={gender}
                    checked={draft.demographics.gender === gender}
                    onChange={() => updateDemographic("gender", gender)}
                  />
                  <span>{gender}</span>
                </label>
              ))}
            </div>
            {renderFieldError("demographics.gender")}
          </fieldset>
          <label className="field">
            <span>
              {interfaceCopy.pretest.idNumber}
              <small>{interfaceCopy.pretest.idNumberNote}</small>
            </span>
            <input
              type="text"
              value={draft.demographics.idNumber ?? ""}
              onChange={(event) => updateDemographic("idNumber", event.target.value)}
            />
            {renderFieldError("demographics.idNumber")}
          </label>
        </div>
      ) : null}

      {!isRestoring && step === "scales" ? (
        <div className="pretest-scale-stack">
          {pretestSections.map((section, index) => (
            <section className="pretest-section-block" key={`section-${index}`}>
              {section.instruction ? <p className="pretest-section-instruction">{section.instruction}</p> : null}
              <div>{section.items.map((item) => renderScaleItem(item))}</div>
            </section>
          ))}
        </div>
      ) : null}

      {!isRestoring && step === "save" ? (
        <p className="pretest-text pretest-save-text">
          {saveMessage ?? interfaceCopy.pretest.saveSuccess}
        </p>
      ) : null}

      {!isRestoring && step !== "save" && saveMessage ? (
        <p className="status-inline">{saveMessage}</p>
      ) : null}
      {!isRestoring && autosaveMessage ? (
        <p className={autosaveMessage.includes("失败") ? "status-inline status-inline--error" : "status-inline"}>
          {isAutosaving ? "正在自动保存…" : autosaveMessage}
        </p>
      ) : null}
      {displayedErrorMessage ? (
        <p className="status-inline status-inline--error">{displayedErrorMessage}</p>
      ) : null}

      {!isRestoring ? (
        <div className="flow-actions flow-actions--stack">
          {step === "save" ? (
            <button
              className="primary-button"
              type="button"
              disabled={isSaving || isEnteringExperiment || !finalResponse}
              onClick={() => void enterExperiment()}
            >
              {interfaceCopy.pretest.enterExperiment}
            </button>
          ) : (
            <button className="primary-button" type="button" onClick={goNext} disabled={isSaving}>
              {step === "intro"
                ? interfaceCopy.pretest.fill
                : step === "demographics"
                  ? interfaceCopy.pretest.next
                  : isSaving
                    ? "保存中"
                    : interfaceCopy.pretest.enterExperiment}
            </button>
          )}
        </div>
      ) : null}

      <div className="flow-footer">浙江大学·人机交互研究</div>
    </form>
  );
}

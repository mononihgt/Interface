import type { PreferenceDecisionPayload } from "../../api/types";

interface PreferenceDecisionPanelProps {
  payload?: unknown;
}

export function PreferenceDecisionPanel({ payload }: PreferenceDecisionPanelProps) {
  const preference = payload as PreferenceDecisionPayload | undefined;
  const options = Array.isArray(preference?.options) ? preference.options : [];
  const preferences = Array.isArray(preference?.preferences) ? preference.preferences : [];

  if (!options.length) {
    return (
      <section className="panel artifact-panel">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">Task Artifact</p>
            <h2>偏好决策卡</h2>
          </div>
        </div>
        <div className="empty-state">当前轮未返回可展示的偏好决策 artifact。</div>
      </section>
    );
  }

  return (
    <section className="panel artifact-panel">
      <div className="panel-heading">
        <div>
          <p className="panel-kicker">Task Artifact</p>
          <h2>偏好决策卡</h2>
        </div>
      </div>
      <div className="artifact-summary-grid">
        <article className="artifact-summary-box">
          <span>当前状态</span>
          <strong>{preference?.mood ?? "未提供"}</strong>
        </article>
        <article className="artifact-summary-box">
          <span>AI 偏好</span>
          <strong>{preference?.ai_preference?.option_id ?? "未提供"}</strong>
        </article>
      </div>
      {preferences.length ? (
        <ul className="plain-list">
          {preferences.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : null}
      <div className="artifact-stack">
        {options.map((option) => (
          <article className="artifact-block" key={option.id ?? option.title ?? "option"}>
            <strong>{option.title ?? option.id ?? "未命名选项"}</strong>
            <p>{Array.isArray(option.signals) ? option.signals.join(" / ") : "暂无线索"}</p>
          </article>
        ))}
      </div>
      {preference?.friend_like_reason ? <p className="muted">{preference.friend_like_reason}</p> : null}
    </section>
  );
}

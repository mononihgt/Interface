import type { CopyVersionsPayload } from "../../api/types";

interface CopyEditorWorkspaceProps {
  payload?: unknown;
}

export function CopyEditorWorkspace({ payload }: CopyEditorWorkspaceProps) {
  const copy = payload as CopyVersionsPayload | undefined;
  const versions = Array.isArray(copy?.versions) ? copy.versions : [];
  const notes = Array.isArray(copy?.revision_notes) ? copy.revision_notes : [];

  if (!versions.length) {
    return (
      <section className="panel artifact-panel">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">Task Artifact</p>
            <h2>文案版本</h2>
          </div>
        </div>
        <div className="empty-state">当前轮未返回可展示的文案版本 artifact。</div>
      </section>
    );
  }

  return (
    <section className="panel artifact-panel">
      <div className="panel-heading">
        <div>
          <p className="panel-kicker">Task Artifact</p>
          <h2>文案版本</h2>
        </div>
      </div>
      <div className="artifact-stack">
        {versions.map((version) => {
          const isSelected = version.id && version.id === copy?.selected_version?.version_id;
          return (
            <article className={`artifact-block${isSelected ? " is-selected" : ""}`} key={version.id ?? version.label ?? "copy"}>
              <div className="artifact-block__header">
                <strong>{version.label ?? "未命名版本"}</strong>
                {isSelected ? <span className="tag">推荐</span> : null}
              </div>
              <p>{version.text ?? "暂无文案内容。"}</p>
            </article>
          );
        })}
      </div>
      {copy?.selected_version?.reason ? <p className="muted">推荐理由：{copy.selected_version.reason}</p> : null}
      {notes.length ? (
        <ul className="plain-list">
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

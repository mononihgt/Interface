import type { DecisionMatrixPayload } from "../../api/types";

interface DecisionMatrixPanelProps {
  payload?: unknown;
}

export function DecisionMatrixPanel({ payload }: DecisionMatrixPanelProps) {
  const matrix = payload as DecisionMatrixPayload | undefined;
  const options = Array.isArray(matrix?.options) ? matrix.options : [];
  const reasons = Array.isArray(matrix?.reasons) ? matrix.reasons : [];

  if (!options.length) {
    return (
      <section className="panel artifact-panel">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">Task Artifact</p>
            <h2>决策矩阵</h2>
          </div>
        </div>
        <div className="empty-state">当前轮未返回可展示的决策矩阵 artifact。</div>
      </section>
    );
  }

  const attributeKeys = Array.from(
    new Set(options.flatMap((option) => Object.keys(option.attributes ?? {}))),
  );

  return (
    <section className="panel artifact-panel">
      <div className="panel-heading">
        <div>
          <p className="panel-kicker">Task Artifact</p>
          <h2>决策矩阵</h2>
        </div>
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>方案</th>
              {attributeKeys.map((key) => (
                <th key={key}>{key}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {options.map((option) => (
              <tr key={option.id ?? option.label ?? "option"}>
                <td>{option.label ?? option.id ?? "-"}</td>
                {attributeKeys.map((key) => (
                  <td key={key}>{String(option.attributes?.[key] ?? "-")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {matrix?.recommendation ? (
        <p className="muted">推荐方案：{String(matrix.recommendation.option_id ?? matrix.recommendation.summary ?? "-")}</p>
      ) : null}
      {reasons.length ? (
        <ul className="plain-list">
          {reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

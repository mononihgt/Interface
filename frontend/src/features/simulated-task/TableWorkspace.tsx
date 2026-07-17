import type { TableArtifactPayload } from "../../api/types";

interface TableWorkspaceProps {
  payload?: unknown;
}

export function TableWorkspace({ payload }: TableWorkspaceProps) {
  const table = payload as TableArtifactPayload | undefined;
  const columns = Array.isArray(table?.columns) ? table.columns : [];
  const rows = Array.isArray(table?.rows) ? table.rows : [];

  if (!columns.length || !rows.length) {
    return (
      <section className="panel artifact-panel">
        <div className="panel-heading">
          <div>
            <p className="panel-kicker">Task Artifact</p>
            <h2>执行表格</h2>
          </div>
        </div>
        <div className="empty-state">当前轮未返回可展示的表格 artifact。</div>
      </section>
    );
  }

  return (
    <section className="panel artifact-panel">
      <div className="panel-heading">
        <div>
          <p className="panel-kicker">Task Artifact</p>
          <h2>执行表格</h2>
        </div>
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={`${index}-${String(row[columns[0]] ?? "")}`}>
                {columns.map((column) => (
                  <td key={column}>{String(row[column] ?? "-")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {table?.errorInjected ? <p className="muted">该 artifact 包含计划性错误注入标记。</p> : null}
    </section>
  );
}

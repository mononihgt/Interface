import { BarChart3 } from "lucide-react";

export function AnalysisPlaceholderSection() {
  return (
    <div className="admin-section-stack">
      <header className="admin-section-header">
        <div>
          <p className="admin-kicker">Analysis</p>
          <h1>数据分析</h1>
        </div>
      </header>
      <section className="admin-empty-state">
        <BarChart3 size={24} />
        <h2>后续接入笔记本分析</h2>
        <p>当前 dashboard 先保留运营、导出和实验控制功能。</p>
      </section>
    </div>
  );
}

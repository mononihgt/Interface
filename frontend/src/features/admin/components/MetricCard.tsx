import type { ReactNode } from "react";

interface MetricCardProps {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: "default" | "good" | "warning" | "danger";
}

export function MetricCard({
  label,
  value,
  detail,
  tone = "default",
}: MetricCardProps) {
  return (
    <article className={`admin-metric-card admin-metric-card--${tone}`}>
      <div className="admin-metric-label">{label}</div>
      <div className="admin-metric-value">{value}</div>
      {detail ? <div className="admin-metric-detail">{detail}</div> : null}
    </article>
  );
}

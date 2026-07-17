interface StatusBadgeProps {
  children: string | number;
  tone?: "neutral" | "good" | "warning" | "danger" | "info";
}

export function StatusBadge({ children, tone = "neutral" }: StatusBadgeProps) {
  return <span className={`admin-status-badge admin-status-badge--${tone}`}>{children}</span>;
}

export function statusTone(status: string | null | undefined): StatusBadgeProps["tone"] {
  if (!status) {
    return "neutral";
  }
  if (["success", "succeeded", "completed", "eligible", "ok", "active"].includes(status)) {
    return "good";
  }
  if (["queued", "running", "started", "review_needed"].includes(status)) {
    return "info";
  }
  if (["timeout", "local_fallback", "abandoned", "excluded"].includes(status)) {
    return "warning";
  }
  if (["failed", "http_error", "invalid_response", "blocked", "invalid"].includes(status)) {
    return "danger";
  }
  return "neutral";
}

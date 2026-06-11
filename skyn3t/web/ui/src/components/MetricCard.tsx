import type { ReactNode } from "react";

export default function MetricCard({
  label,
  value,
  sub,
  icon,
  accent = "cyan",
  className = "",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  icon?: string;
  accent?: "cyan" | "amber" | "green";
  className?: string;
}) {
  const accentClass =
    accent === "amber"
      ? "text-amber"
      : accent === "green"
        ? "text-status-green"
        : "text-accent";

  return (
    <div className={["panel-card p-4 flex flex-col gap-2 min-w-0", className].join(" ")}>
      <div className="flex items-center gap-2 section-label">
        {icon && <i className={[icon, accentClass, "opacity-80"].join(" ")} />}
        <span>{label}</span>
      </div>
      <div className={["font-mono text-2xl font-medium tracking-tight", accentClass].join(" ")}>
        {value}
      </div>
      {sub && <div className="text-text-dim text-xs font-mono truncate">{sub}</div>}
    </div>
  );
}

export function StatRow({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1 border-b border-border/50 last:border-0">
      <span className="text-text-secondary text-sm">{label}</span>
      <span className="font-mono text-accent text-base shrink-0">{value}</span>
    </div>
  );
}

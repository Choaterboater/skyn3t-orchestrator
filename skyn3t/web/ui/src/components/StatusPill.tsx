type StatusTone = "success" | "warning" | "error" | "active" | "neutral";

const TONE_CLASSES: Record<StatusTone, string> = {
  success: "bg-status-green/10 border-status-green/30 text-status-green",
  warning: "bg-status-yellow/10 border-status-yellow/30 text-status-yellow",
  error: "bg-status-red/10 border-status-red/30 text-status-red",
  active: "bg-accent-soft border-accent-line text-accent",
  neutral: "bg-bg-3 border-border text-text-secondary",
};

function toneForStatus(status: string): StatusTone {
  switch (status) {
    case "idle":
    case "online":
    case "ok":
    case "passed":
      return "success";
    case "busy":
    case "building":
    case "learning":
    case "running":
      return "active";
    case "error":
    case "failed":
    case "offline":
      return "error";
    case "disabled":
    case "pending":
      return "warning";
    default:
      return "neutral";
  }
}

export default function StatusPill({
  status,
  label,
  pulse,
}: {
  status: string;
  label?: string;
  pulse?: boolean;
}) {
  const tone = toneForStatus(status);
  return (
    <span
      className={[
        "inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[0.6rem] uppercase tracking-wider border font-medium",
        TONE_CLASSES[tone],
      ].join(" ")}
    >
      {pulse && tone === "active" && <span className="live-dot shrink-0" />}
      {label ?? status}
    </span>
  );
}

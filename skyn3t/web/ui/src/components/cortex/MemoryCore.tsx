import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../../api/client";
import { ProgressRing } from "../Sparkline";

// ============================================================
// MemoryCore — the swarm's living memory, rendered as a breathing core.
//
// Concentric rings map the three memory layers (session -> operator ->
// project). The project success_rate drives a ProgressRing; the core's
// brightness/glow scales with accumulated knowledge:
//   total_insights + operator.insight_count + top_skills.length.
// Below the core, recent insights and top skills accumulate so the
// operator can watch the system learn in real time.
// ============================================================

export interface MemoryCoreProps {
  className?: string;
}

/**
 * Brightness 0..1 from accumulated cognition signals. Saturates with a
 * soft log so the core keeps glowing brighter as memory grows but never
 * clips. PURE-ish (no side effects). Exported for potential reuse/test.
 */
export function coreBrightness(
  totalInsights: number,
  operatorInsightCount: number,
  topSkillCount: number,
): number {
  const signal = totalInsights + operatorInsightCount + topSkillCount * 2;
  if (signal <= 0) return 0;
  // log curve: ~0.5 at 8 signals, ~0.85 at 40, asymptotic to 1.
  return Math.min(1, Math.log10(1 + signal) / Math.log10(40));
}

export default function MemoryCore({ className = "" }: MemoryCoreProps) {
  const consciousness = useQuery({
    queryKey: ["consciousness_status"],
    queryFn: api.consciousnessStatus,
    refetchInterval: 8_000,
  });
  const layers = useQuery({
    queryKey: ["memory_layers"],
    queryFn: () => api.memoryLayers(8),
    refetchInterval: 8_000,
  });

  const enabled =
    consciousness.data?.enabled === true || layers.data?.enabled === true;

  const totalInsights = consciousness.data?.total_insights ?? 0;
  const agentsWithInsights = consciousness.data?.agents_with_insights ?? [];
  const operator = layers.data?.layers?.operator;
  const project = layers.data?.layers?.project;
  const session = layers.data?.layers?.session;

  const operatorInsightCount = operator?.insight_count ?? 0;
  const topSkills = operator?.top_skills ?? [];
  const recentInsights = operator?.recent_insights ?? [];
  const successRate = project?.success_rate ?? 0;

  const brightness = useMemo(
    () => coreBrightness(totalInsights, operatorInsightCount, topSkills.length),
    [totalInsights, operatorInsightCount, topSkills.length],
  );

  const loading = consciousness.isLoading && layers.isLoading;

  if (!loading && !enabled) {
    return (
      <div className={["flex flex-col", className].join(" ")}>
        <CoreHeader brightness={0} />
        <div className="flex-1 flex items-center justify-center p-6 text-center">
          <div className="space-y-1">
            <div className="text-text-secondary text-sm">
              Consciousness layer is offline.
            </div>
            <div className="text-text-dim text-xs font-mono max-w-xs">
              Enable consciousness/memory in the orchestrator to watch the core
              accumulate insights and skills.
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={["flex flex-col min-h-0", className].join(" ")}>
      <CoreHeader brightness={brightness} />

      <div className="flex-1 min-h-0 overflow-y-auto">
        {/* The core */}
        <div className="flex flex-col items-center pt-5 pb-3">
          <CoreVisual
            brightness={brightness}
            successRate={successRate}
            sessionCount={session?.active_sessions ?? 0}
            operatorCount={operatorInsightCount}
          />
          <div className="mt-3 flex flex-wrap justify-center gap-2 px-3">
            <CoreStat
              label="insights"
              value={totalInsights || operatorInsightCount}
            />
            <CoreStat label="skills" value={topSkills.length} tone="amber" />
            <CoreStat
              label="sessions"
              value={session?.active_sessions ?? 0}
              tone="dim"
            />
          </div>
        </div>

        {/* Layer readout */}
        <div className="px-3 grid grid-cols-3 gap-2 mb-3">
          <LayerCell
            name="session"
            primary={session?.active_sessions ?? 0}
            secondary={`${session?.sessions?.length ?? 0} tracked`}
          />
          <LayerCell
            name="operator"
            primary={operatorInsightCount}
            secondary={`${agentsWithInsights.length} agents`}
            tone="amber"
          />
          <LayerCell
            name="project"
            primary={`${Math.round(successRate * 100)}%`}
            secondary={`${project?.tasks ?? 0} tasks`}
            tone="accent"
          />
        </div>

        {/* Top skills */}
        <Section title="Top skills" count={topSkills.length}>
          {topSkills.length === 0 ? (
            <Empty text="No skills mastered yet." />
          ) : (
            <ul className="space-y-1">
              {topSkills.slice(0, 6).map((s, i) => (
                <li
                  key={`${s.name}-${i}`}
                  className="flex items-center gap-2 text-xs"
                >
                  <span className="text-text-primary truncate flex-1">
                    {s.name}
                  </span>
                  <ScoreBar score={s.score} />
                  <span className="font-mono text-text-dim w-9 text-right shrink-0">
                    {formatScore(s.score)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Section>

        {/* Recent insights */}
        <Section title="Recent insights" count={recentInsights.length}>
          {recentInsights.length === 0 ? (
            <Empty text="No insights recorded yet." />
          ) : (
            <ul className="space-y-1.5">
              {recentInsights.slice(0, 8).map((ins, i) => (
                <li
                  key={i}
                  className="text-xs border-l-2 border-amber-line pl-2 py-0.5"
                >
                  <div className="text-text-primary break-words leading-snug">
                    {ins.insight ?? "(insight)"}
                  </div>
                  {(ins.agent || ins.capability) && (
                    <div className="text-[0.6rem] font-mono text-text-dim mt-0.5 truncate">
                      {ins.agent}
                      {ins.agent && ins.capability && " · "}
                      {ins.capability}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Section>
      </div>
    </div>
  );
}

function CoreHeader({ brightness }: { brightness: number }) {
  return (
    <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-border bg-bg-3/60 shrink-0">
      <div className="flex items-center gap-2">
        <i className="fa-solid fa-circle-nodes text-accent text-xs" aria-hidden />
        <span className="section-label">Memory Core</span>
      </div>
      <span
        className="text-[0.6rem] font-mono uppercase tracking-wider text-text-dim"
        title="Core brightness scales with accumulated insights + skills"
      >
        {Math.round(brightness * 100)}% lit
      </span>
    </div>
  );
}

function CoreVisual({
  brightness,
  successRate,
  sessionCount,
  operatorCount,
}: {
  brightness: number;
  successRate: number;
  sessionCount: number;
  operatorCount: number;
}) {
  // Outer ring: project success_rate (the contract-mandated ProgressRing).
  // Inner concentric rings: session + operator fill, scaled to a soft cap.
  const sessionFill = Math.min(1, sessionCount / 6);
  const operatorFill = Math.min(1, operatorCount / 24);
  // Glow scales with brightness; reduced-motion handled by ProgressRing glow prop.
  const glowAlpha = (0.15 + brightness * 0.5).toFixed(3);
  const coreColor = "#38d4f0";
  return (
    <div
      className="relative grid place-items-center"
      style={{ width: 176, height: 176 }}
    >
      {/* Ambient bloom — brighter as memory grows */}
      <div
        aria-hidden
        className="absolute inset-0 rounded-full pointer-events-none"
        style={{
          background: `radial-gradient(circle, rgba(56,212,240,${glowAlpha}) 0%, transparent 68%)`,
        }}
      />
      {/* Outer: project success ring */}
      <ProgressRing
        value={successRate}
        size={176}
        thickness={7}
        color="#38d4f0"
        trackColor="rgba(56,212,240,0.10)"
        glow={brightness > 0.4}
        aria-label={`Project success rate ${Math.round(successRate * 100)} percent`}
        className="text-accent"
      />
      {/* Middle: operator insight ring */}
      <div className="absolute">
        <ProgressRing
          value={operatorFill}
          size={130}
          thickness={5}
          color="#e5a045"
          trackColor="rgba(229,160,69,0.10)"
          aria-label={`Operator memory fill ${Math.round(operatorFill * 100)} percent`}
        />
      </div>
      {/* Inner: session ring */}
      <div className="absolute">
        <ProgressRing
          value={sessionFill}
          size={86}
          thickness={4}
          color="#9aa8b8"
          trackColor="rgba(154,168,184,0.10)"
          aria-label={`Active session memory ${Math.round(sessionFill * 100)} percent`}
        />
      </div>
      {/* Core dot */}
      <div
        aria-hidden
        className="absolute rounded-full"
        style={{
          width: 26,
          height: 26,
          background: coreColor,
          opacity: 0.35 + brightness * 0.65,
          boxShadow: `0 0 ${8 + brightness * 28}px rgba(56,212,240,${(
            0.4 +
            brightness * 0.5
          ).toFixed(3)})`,
        }}
      />
      <div className="absolute text-center pointer-events-none">
        <div className="font-mono text-lg text-accent leading-none mt-[3.1rem]">
          {Math.round(successRate * 100)}%
        </div>
        <div className="text-[0.55rem] uppercase tracking-wider text-text-dim">
          success
        </div>
      </div>
    </div>
  );
}

function CoreStat({
  label,
  value,
  tone = "accent",
}: {
  label: string;
  value: number;
  tone?: "accent" | "amber" | "dim";
}) {
  const color =
    tone === "amber"
      ? "text-amber"
      : tone === "dim"
        ? "text-text-secondary"
        : "text-accent";
  return (
    <div className="flex items-baseline gap-1.5 rounded border border-border bg-bg-3/50 px-2 py-1">
      <span className={["font-mono text-sm", color].join(" ")}>{value}</span>
      <span className="text-[0.58rem] uppercase tracking-wider text-text-dim">
        {label}
      </span>
    </div>
  );
}

function LayerCell({
  name,
  primary,
  secondary,
  tone = "dim",
}: {
  name: string;
  primary: React.ReactNode;
  secondary: string;
  tone?: "accent" | "amber" | "dim";
}) {
  const ring =
    tone === "amber"
      ? "border-amber-line"
      : tone === "accent"
        ? "border-accent-line"
        : "border-border";
  const color =
    tone === "amber"
      ? "text-amber"
      : tone === "accent"
        ? "text-accent"
        : "text-text-primary";
  return (
    <div className={["rounded border bg-bg-3/40 px-2 py-1.5", ring].join(" ")}>
      <div className="text-[0.55rem] uppercase tracking-wider text-text-dim">
        {name}
      </div>
      <div className={["font-mono text-base leading-tight", color].join(" ")}>
        {primary}
      </div>
      <div className="text-[0.58rem] font-mono text-text-dim truncate">
        {secondary}
      </div>
    </div>
  );
}

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <div className="px-3 py-2 border-t border-border/60">
      <div className="flex items-center justify-between mb-1.5">
        <span className="section-label">{title}</span>
        <span className="text-[0.6rem] font-mono text-text-dim">{count}</span>
      </div>
      {children}
    </div>
  );
}

function ScoreBar({ score }: { score: number }) {
  const pct = Math.max(0, Math.min(100, score * 100));
  return (
    <div
      className="h-1 w-16 rounded-full bg-bg-3 overflow-hidden shrink-0"
      aria-hidden
    >
      <div
        className="h-full rounded-full bg-amber"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="text-[0.7rem] text-text-dim font-mono py-1">{text}</div>;
}

function formatScore(score: number): string {
  if (!isFinite(score)) return "—";
  return score.toFixed(2);
}

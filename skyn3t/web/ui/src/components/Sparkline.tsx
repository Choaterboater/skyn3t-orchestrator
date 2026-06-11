// SkyN3t — Command Center Atelier chart primitives.
//
// Three zero-dependency, hand-rolled inline-SVG visualizations that share
// ONE file (no extra owned files): Sparkline (default), MiniBars, ProgressRing.
//
// Design intent: these are "instrument readouts", not generic cards. They lean
// on the atelier palette via `currentColor` so callers tint with Tailwind
// text-* classes (text-accent / text-amber). No intrinsic animation — parents
// layer reduced-motion-aware pulses via CSS. Every primitive renders
// role="img" + aria-label, degrades to a calm baseline on empty/NaN data, and
// never throws.

import type { ReactNode } from "react";

/* ============================================================
   PURE GEOMETRY HELPERS (tested in charts.test.ts, no DOM)
   ============================================================ */

/** Numbers that are finite (not NaN / ±Infinity). */
function isFiniteNum(n: unknown): n is number {
  return typeof n === "number" && Number.isFinite(n);
}

/** Keep only finite numbers from an arbitrary input array. */
function sanitize(values: readonly number[] | null | undefined): number[] {
  if (!Array.isArray(values)) return [];
  return values.filter(isFiniteNum);
}

/**
 * Build the `points` attribute for an SVG <polyline> from a series of values.
 *
 * - Maps the value domain [min..max] onto the pixel range [h-pad .. pad]
 *   (SVG y grows downward, so the largest value sits near the top).
 * - When all values are equal (or domain is degenerate) the line is drawn
 *   flat through the vertical center — a stable, readable baseline.
 * - Empty input yields a single centered baseline segment (two points) so the
 *   consuming <polyline> still renders something rather than collapsing.
 * - NaN/Infinity entries are dropped defensively before mapping.
 *
 * PURE: no DOM, deterministic. Returns a string like "0,14 60,3 120,27".
 */
export function buildPolylinePoints(
  values: readonly number[] | null | undefined,
  width: number,
  height: number,
  min?: number,
  max?: number,
): string {
  const w = isFiniteNum(width) && width > 0 ? width : 1;
  const h = isFiniteNum(height) && height > 0 ? height : 1;
  const clean = sanitize(values);

  // Small vertical padding so the stroke never clips at the edges.
  const pad = Math.min(h / 2, Math.max(1, h * 0.08));
  const top = pad;
  const bottom = h - pad;
  const midY = round(h / 2);

  if (clean.length === 0) {
    return `0,${midY} ${round(w)},${midY}`;
  }
  if (clean.length === 1) {
    // Single sample → flat line at vertical center.
    return `0,${midY} ${round(w)},${midY}`;
  }

  const lo = isFiniteNum(min) ? min : Math.min(...clean);
  const hi = isFiniteNum(max) ? max : Math.max(...clean);
  const span = hi - lo;

  const stepX = w / (clean.length - 1);

  return clean
    .map((v, i) => {
      const x = round(i * stepX);
      let y: number;
      if (span <= 0) {
        y = midY;
      } else {
        const t = (v - lo) / span; // 0..1, higher value = larger t
        y = round(bottom - t * (bottom - top));
      }
      return `${x},${y}`;
    })
    .join(" ");
}

/**
 * stroke-dasharray pair for a progress arc on a circle of radius `r`.
 * Returns `[filled, gap]` where filled = value*circumference.
 * value is clamped to 0..1; non-finite → 0. PURE.
 */
export function arcDashArray(value: number, r: number): [number, number] {
  const radius = isFiniteNum(r) && r > 0 ? r : 0;
  const circumference = 2 * Math.PI * radius;
  const v = clamp01(value);
  const filled = round(circumference * v);
  const gap = round(circumference - filled);
  return [filled, gap];
}

/** Clamp to [0,1]; non-finite → 0. */
export function clamp01(value: number): number {
  if (!isFiniteNum(value)) return 0;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

/** Round to 2 decimals to keep SVG attribute strings compact & stable.
 *  `+ 0` normalizes negative-zero so callers/tests see a plain 0. */
function round(n: number): number {
  return Math.round(n * 100) / 100 + 0;
}

/* ============================================================
   Sparkline (default export)
   ============================================================ */

export interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  strokeWidth?: number;
  /** Explicit stroke color override; defaults to currentColor via CSS var. */
  color?: string;
  /** Render a soft gradient area under the line. */
  fill?: boolean;
  min?: number;
  max?: number;
  "aria-label"?: string;
  className?: string;
}

let gradientSeq = 0;
/** Stable-per-instance id without pulling in useId (keeps tree dep-free). */
function nextGradientId(): string {
  gradientSeq += 1;
  return `spark-grad-${gradientSeq}`;
}

export default function Sparkline({
  values,
  width = 120,
  height = 28,
  strokeWidth = 1.5,
  color,
  fill = false,
  min,
  max,
  className = "",
  "aria-label": ariaLabel,
}: SparklineProps): JSX.Element {
  const clean = sanitize(values);
  const points = buildPolylinePoints(values, width, height, min, max);
  const stroke = color ?? "var(--spark, currentColor)";
  const gradId = fill ? nextGradientId() : undefined;

  // Build a closed area path by anchoring the polyline down to the baseline.
  // Reuse the same "x,y x,y ..." point list so line + fill never diverge.
  const pairs = points.split(" ").filter(Boolean);
  const areaPath =
    fill && clean.length >= 2 && pairs.length >= 2
      ? `M${pairs[0]} ${pairs
          .slice(1)
          .map((p) => `L${p}`)
          .join(" ")} L${round(width)},${round(height)} L0,${round(height)} Z`
      : undefined;

  const label =
    ariaLabel ??
    (clean.length === 0
      ? "Sparkline, no data"
      : `Sparkline trend, ${clean.length} points`);

  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      preserveAspectRatio="none"
      className={["overflow-visible", className].join(" ")}
    >
      {fill && gradId && (
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0} />
          </linearGradient>
        </defs>
      )}
      {areaPath && <path d={areaPath} fill={`url(#${gradId})`} stroke="none" />}
      <polyline
        points={points}
        fill="none"
        stroke={stroke}
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
      {clean.length === 0 && (
        // Faint baseline marker so an empty series reads as "idle", not broken.
        <line
          x1={0}
          y1={round(height / 2)}
          x2={round(width)}
          y2={round(height / 2)}
          stroke="currentColor"
          strokeWidth={1}
          strokeDasharray="2 3"
          opacity={0.3}
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  );
}

/* ============================================================
   MiniBars (named export)
   ============================================================ */

export interface MiniBarsProps {
  values: number[];
  labels?: string[];
  height?: number;
  gap?: number;
  color?: string;
  max?: number;
  highlightIndex?: number;
  "aria-label"?: string;
  className?: string;
}

export function MiniBars({
  values,
  labels,
  height = 40,
  gap = 2,
  color,
  max,
  highlightIndex,
  className = "",
  "aria-label": ariaLabel,
}: MiniBarsProps): JSX.Element {
  const clean = sanitize(values);
  const count = clean.length;
  // Virtual coordinate space; preserveAspectRatio='none' stretches to fit.
  const slot = 12;
  const width = Math.max(slot, count * slot);
  const fill = color ?? "currentColor";

  const hi = isFiniteNum(max) ? max : count > 0 ? Math.max(...clean, 0) : 0;
  const denom = hi > 0 ? hi : 1;
  const minBarH = 1; // always show a sliver so zero-values are still visible

  const label =
    ariaLabel ??
    (count === 0 ? "Bar chart, no data" : `Bar chart, ${count} values`);

  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${width} ${height}`}
      width="100%"
      height={height}
      preserveAspectRatio="none"
      className={["block", className].join(" ")}
    >
      {count === 0 ? (
        <line
          x1={0}
          y1={height - 0.5}
          x2={width}
          y2={height - 0.5}
          stroke="currentColor"
          strokeWidth={1}
          strokeDasharray="2 3"
          opacity={0.3}
          vectorEffect="non-scaling-stroke"
        />
      ) : (
        clean.map((v, i) => {
          const t = clamp01(v / denom);
          const barH = Math.max(minBarH, round(t * (height - 1)));
          const x = round(i * slot + gap / 2);
          const w = round(slot - gap);
          const y = round(height - barH);
          const isHi = i === highlightIndex;
          return (
            <rect
              key={i}
              x={x}
              y={y}
              width={w > 0 ? w : 1}
              height={barH}
              rx={1}
              fill={fill}
              className={isHi ? "text-accent-strong" : undefined}
              opacity={isHi ? 1 : 0.65}
            >
              {labels && labels[i] != null && <title>{labels[i]}</title>}
            </rect>
          );
        })
      )}
    </svg>
  );
}

/* ============================================================
   ProgressRing (named export)
   ============================================================ */

export interface ProgressRingProps {
  value: number; // 0..1
  size?: number;
  thickness?: number;
  trackColor?: string;
  color?: string;
  label?: ReactNode; // centered content
  "aria-label"?: string;
  glow?: boolean;
  className?: string;
}

let glowSeq = 0;
function nextGlowId(): string {
  glowSeq += 1;
  return `ring-glow-${glowSeq}`;
}

export function ProgressRing({
  value,
  size = 64,
  thickness = 6,
  trackColor,
  color,
  label,
  glow = false,
  className = "",
  "aria-label": ariaLabel,
}: ProgressRingProps): JSX.Element {
  const v = clamp01(value);
  const sz = isFiniteNum(size) && size > 0 ? size : 64;
  const th = isFiniteNum(thickness) && thickness > 0 ? thickness : 6;
  const r = Math.max(0, sz / 2 - th / 2);
  const cx = sz / 2;
  const cy = sz / 2;
  const [filled, gap] = arcDashArray(v, r);
  const stroke = color ?? "currentColor";
  const track = trackColor ?? "rgba(138, 155, 176, 0.18)";
  const pct = Math.round(v * 100);
  const glowId = glow ? nextGlowId() : undefined;

  const label2 =
    ariaLabel ?? `Progress ${pct} percent`;

  return (
    <div
      className={["relative inline-grid place-items-center", className].join(" ")}
      style={{ width: sz, height: sz }}
    >
      <svg
        role="img"
        aria-label={label2}
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        viewBox={`0 0 ${sz} ${sz}`}
        width={sz}
        height={sz}
        className="block -rotate-90"
      >
        {glow && glowId && (
          <defs>
            <filter id={glowId} x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation={th * 0.6} result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>
        )}
        {/* Track */}
        <circle
          cx={cx}
          cy={cy}
          r={r}
          fill="none"
          stroke={track}
          strokeWidth={th}
        />
        {/* Progress arc */}
        <circle
          cx={cx}
          cy={cy}
          r={r}
          fill="none"
          stroke={stroke}
          strokeWidth={th}
          strokeLinecap="round"
          strokeDasharray={`${filled} ${gap}`}
          filter={glow && glowId ? `url(#${glowId})` : undefined}
        />
      </svg>
      {label != null && (
        <div className="absolute inset-0 grid place-items-center text-center leading-none pointer-events-none">
          {label}
        </div>
      )}
    </div>
  );
}

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

// Renders the build-pattern scoreboard: per-stack best/worst shape
// with success rates. Lets you click a stack to see every recorded
// shape for it.
export default function BuildPatternsPage() {
  const [stack, setStack] = useState<string | null>(null);
  const { data: summary } = useQuery({
    queryKey: ["build_patterns"],
    queryFn: () => api.buildPatterns(),
    refetchInterval: 30_000,
  });
  const { data: stackDetail } = useQuery({
    queryKey: ["build_patterns_stack", stack],
    queryFn: () => api.buildPatterns(stack!),
    enabled: !!stack,
  });

  const per: Record<string, any> = summary?.per_stack ?? {};
  const stacks = Object.keys(per);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="display text-4xl">
          <span className="text-accent">Build Patterns</span>
        </h1>
        <p className="text-text-secondary text-sm mt-1">
          What scaffold shapes are actually building — captured per stack from
          BuildVerifier outcomes.
        </p>
      </header>

      {stacks.length === 0 && (
        <p className="text-text-secondary">
          No data yet. The scoreboard fills in as scaffolds complete + verify.
        </p>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {stacks.map((s) => {
          const entry = per[s];
          return (
            <div
              key={s}
              onClick={() => setStack(stack === s ? null : s)}
              className={[
                "rounded-lg border bg-bg-2 p-4 cursor-pointer transition",
                stack === s
                  ? "border-accent"
                  : "border-border hover:border-border-strong",
              ].join(" ")}
            >
              <h3 className="font-mono text-accent mb-3">{s}</h3>
              <ShapeRow label="best"  shape={entry.best} />
              <ShapeRow label="worst" shape={entry.worst} />
            </div>
          );
        })}
      </div>

      {stack && stackDetail?.shapes && (
        <section className="space-y-2">
          <h2 className="text-lg font-medium">
            All shapes for <span className="text-accent font-mono">{stack}</span>
          </h2>
          <div className="rounded-lg border border-border bg-bg-2 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-bg-3 text-text-secondary text-xs uppercase tracking-wider">
                <tr>
                  <th className="px-4 py-2 text-left">Shape</th>
                  <th className="px-4 py-2 text-right">Success</th>
                  <th className="px-4 py-2 text-right">Failure</th>
                  <th className="px-4 py-2 text-right">Skipped</th>
                </tr>
              </thead>
              <tbody>
                {stackDetail.shapes.map((sh, idx) => (
                  <tr key={idx} className="border-t border-border">
                    <td className="px-4 py-2 font-mono text-xs">
                      <ShapeBullets paths={sh.shape} />
                    </td>
                    <td className="px-4 py-2 text-right font-mono">{sh.success}</td>
                    <td className="px-4 py-2 text-right font-mono">{sh.failure}</td>
                    <td className="px-4 py-2 text-right font-mono">{sh.skipped}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

function ShapeRow({ label, shape }: { label: string; shape: any | null }) {
  if (!shape) return null;
  const total = shape.success + shape.failure;
  const rate = total === 0 ? "—" : `${Math.round((shape.success / total) * 100)}%`;
  return (
    <div className="text-sm mb-2">
      <div className="flex justify-between items-baseline mb-1">
        <span className="text-text-secondary uppercase tracking-wider text-xs">
          {label}
        </span>
        <span className="font-mono text-accent">
          {rate} · {shape.success}/{total}
        </span>
      </div>
      <ShapeBullets paths={shape.shape ?? []} />
    </div>
  );
}

function ShapeBullets({ paths }: { paths: string[] }) {
  return (
    <ul className="text-xs text-text-secondary list-none">
      {paths.slice(0, 6).map((p) => (
        <li key={p} className="truncate">
          <span className="text-text-dim">·</span> {p}
        </li>
      ))}
      {paths.length > 6 && (
        <li className="text-text-dim">…{paths.length - 6} more</li>
      )}
    </ul>
  );
}

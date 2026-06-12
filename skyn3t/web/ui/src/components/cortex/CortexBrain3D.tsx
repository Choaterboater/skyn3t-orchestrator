import { useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { Html, Line, OrbitControls, Stars } from "@react-three/drei";
import * as THREE from "three";

import type { SwarmEvent } from "../../context/SwarmProvider";
import { useSwarm } from "../../context/SwarmProvider";
import AgentGraph, { edgeFromEvent, type GraphNodeSeed } from "./AgentGraph";

// ============================================================
// CortexBrain3D — the swarm's mind as a living Three.js neural field.
//
// • A central "memory core" (icosahedron) breathes and brightens with
//   activity.  • Agent nodes float on a sphere around it, colored by
//   state (cyan idle / amber busy / red error), sized + lit by traffic.
//   • Edges are recent from->to hops; each live swarm event sends a
//   glowing pulse traveling along its edge — the same data the 2D
//   AgentGraph uses (edgeFromEvent + useSwarm), now in 3D.
//
// Degrades gracefully: no WebGL -> the 2D AgentGraph; prefers-reduced-
// motion -> no auto-rotate and no traveling pulses (static field).
// ============================================================

const C = {
  cyan: "#38d4f0",
  cyanStrong: "#5ee4ff",
  amber: "#e5a045",
  amberStrong: "#f0b45c",
  red: "#ff5d6c",
  dim: "#33414f",
  bg: "#0c0e12",
};

function hasWebGL(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const c = document.createElement("canvas");
    return !!(
      window.WebGLRenderingContext &&
      (c.getContext("webgl") || c.getContext("experimental-webgl"))
    );
  } catch {
    return false;
  }
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    !!window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function stateColor(state?: string): string {
  const s = (state || "").toLowerCase();
  if (s.includes("err") || s.includes("fail")) return C.red;
  if (s.includes("busy") || s.includes("run") || s.includes("work")) return C.amber;
  return C.cyan;
}

// Even point distribution on a sphere (fibonacci) — deterministic so the
// same agents always land in the same place across renders.
function fibonacciSphere(n: number, radius: number): THREE.Vector3[] {
  const pts: THREE.Vector3[] = [];
  if (n <= 0) return pts;
  if (n === 1) return [new THREE.Vector3(0, 0, radius)];
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / (n - 1)) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = golden * i;
    pts.push(
      new THREE.Vector3(Math.cos(theta) * r * radius, y * radius, Math.sin(theta) * r * radius),
    );
  }
  return pts;
}

const easeInOut = (t: number) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

interface Pulse {
  id: number;
  from: THREE.Vector3;
  to: THREE.Vector3;
  color: string;
}

// ---------- 3D primitives ----------

function MemoryCore({ activity, reduced }: { activity: number; reduced: boolean }) {
  const core = useRef<THREE.Mesh>(null);
  const shell = useRef<THREE.Mesh>(null);
  const mat = useRef<THREE.MeshStandardMaterial>(null);
  useFrame(({ clock }) => {
    const t = clock.elapsedTime;
    const glow = 0.5 + Math.min(1, activity) * 1.6;
    if (mat.current) {
      mat.current.emissiveIntensity = reduced ? glow : glow * (0.85 + 0.15 * Math.sin(t * 1.6));
    }
    if (core.current && !reduced) {
      const s = 1 + 0.04 * Math.sin(t * 1.4);
      core.current.scale.setScalar(s);
    }
    if (shell.current) {
      if (!reduced) shell.current.rotation.y = t * 0.12;
      shell.current.rotation.x = reduced ? 0.3 : 0.3 + 0.08 * Math.sin(t * 0.5);
    }
  });
  return (
    <group>
      <mesh ref={core}>
        <icosahedronGeometry args={[0.9, 2]} />
        <meshStandardMaterial
          ref={mat}
          color={C.cyan}
          emissive={C.cyanStrong}
          emissiveIntensity={1.2}
          roughness={0.25}
          metalness={0.4}
        />
      </mesh>
      {/* faint wireframe shell suggesting a skull / field boundary */}
      <mesh ref={shell} scale={1.55}>
        <icosahedronGeometry args={[0.9, 1]} />
        <meshBasicMaterial color={C.cyan} wireframe transparent opacity={0.12} />
      </mesh>
      <pointLight color={C.cyanStrong} intensity={2 + activity * 4} distance={14} decay={2} />
    </group>
  );
}

function AgentNode({
  pos,
  color,
  label,
  busy,
  weight,
  reduced,
}: {
  pos: THREE.Vector3;
  color: string;
  label: string;
  busy: boolean;
  weight: number;
  reduced: boolean;
}) {
  const halo = useRef<THREE.Mesh>(null);
  const r = 0.18 + Math.min(0.14, weight * 0.014);
  useFrame(({ clock }) => {
    if (halo.current && !reduced) {
      const t = clock.elapsedTime;
      const pulse = busy ? 0.5 + 0.5 * Math.sin(t * 4) : 0.25 + 0.1 * Math.sin(t * 1.5);
      (halo.current.material as THREE.MeshBasicMaterial).opacity = 0.08 + pulse * 0.18;
      halo.current.scale.setScalar(1 + pulse * 0.5);
    }
  });
  return (
    <group position={pos}>
      <mesh>
        <sphereGeometry args={[r, 24, 24]} />
        <meshStandardMaterial
          color={color}
          emissive={color}
          emissiveIntensity={busy ? 2.2 : 1.1}
          roughness={0.3}
          metalness={0.2}
        />
      </mesh>
      {/* additive halo for the neon glow without postprocessing */}
      <mesh ref={halo} scale={2.4}>
        <sphereGeometry args={[r, 16, 16]} />
        <meshBasicMaterial color={color} transparent opacity={0.16} blending={THREE.AdditiveBlending} depthWrite={false} />
      </mesh>
      <Html center distanceFactor={9} style={{ pointerEvents: "none" }}>
        <span
          style={{
            fontFamily: '"JetBrains Mono", ui-monospace, monospace',
            fontSize: "11px",
            color: busy ? C.amberStrong : "#8a9bb0",
            whiteSpace: "nowrap",
            textShadow: "0 0 6px rgba(0,0,0,0.9)",
            userSelect: "none",
          }}
        >
          {label}
        </span>
      </Html>
    </group>
  );
}

function TravelingPulse({ pulse, onDone, duration }: { pulse: Pulse; onDone: (id: number) => void; duration: number }) {
  const ref = useRef<THREE.Mesh>(null);
  const start = useRef<number | null>(null);
  useFrame(({ clock }) => {
    if (start.current == null) start.current = clock.elapsedTime;
    const t = (clock.elapsedTime - start.current) / duration;
    if (t >= 1) {
      onDone(pulse.id);
      return;
    }
    if (ref.current) {
      ref.current.position.lerpVectors(pulse.from, pulse.to, easeInOut(t));
      const fade = Math.sin(Math.PI * t); // in then out
      ref.current.scale.setScalar(0.5 + fade);
      (ref.current.material as THREE.MeshBasicMaterial).opacity = fade;
    }
  });
  return (
    <mesh ref={ref}>
      <sphereGeometry args={[0.07, 12, 12]} />
      <meshBasicMaterial color={pulse.color} transparent blending={THREE.AdditiveBlending} depthWrite={false} />
    </mesh>
  );
}

const CORE_POS = new THREE.Vector3(0, 0, 0);

function Scene({ seeds, reduced }: { seeds: GraphNodeSeed[]; reduced: boolean }) {
  const { subscribe } = useSwarm();
  const group = useRef<THREE.Group>(null);
  const [pulses, setPulses] = useState<Pulse[]>([]);
  const [edges, setEdges] = useState<Record<string, [string, string]>>({});
  const [activity, setActivity] = useState(0);
  const pulseId = useRef(0);
  // Live-activity window per agent: any event naming an agent keeps its
  // node lit amber for a few seconds. The 6s snapshot's `state` field
  // misses most cortex work (scout, critiques, ingest never flip an
  // agent to "busy"), so the brain looked asleep while working.
  const activeUntil = useRef<Record<string, number>>({});
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);

  // Stable node positions (sorted for determinism), id -> Vector3.
  const positions = useMemo(() => {
    const ordered = [...seeds].sort((a, b) => a.id.localeCompare(b.id));
    const radius = 3.2 + Math.min(2, ordered.length * 0.04);
    const pts = fibonacciSphere(ordered.length, radius);
    const map = new Map<string, THREE.Vector3>();
    ordered.forEach((s, i) => map.set(s.id, pts[i]));
    return map;
  }, [seeds]);

  // Live wiring. Two-agent events (from+to) add an edge + a node-to-node
  // pulse. Single-agent events — the overwhelming majority (thoughts, LLM
  // exchanges, task starts only carry `from`) — light that agent amber
  // and fire a core->agent pulse so the brain visibly works.
  useEffect(() => {
    const spawnPulse = (a: THREE.Vector3, b: THREE.Vector3) => {
      setPulses((prev) => {
        const next = prev.length > 28 ? prev.slice(prev.length - 28) : prev;
        pulseId.current += 1;
        return [...next, { id: pulseId.current, from: a, to: b, color: C.cyanStrong }];
      });
    };
    const off = subscribe("*", (e: SwarmEvent) => {
      const from = (e.from ?? "").trim();
      const to = (e.to ?? "").trim();
      const known = [from, to].filter((id) => id && positions.has(id));
      if (!known.length) return;
      const stamp = Date.now() + 5000;
      known.forEach((id) => {
        activeUntil.current[id] = stamp;
      });
      setActivity((v) => Math.min(1.5, v + 0.35));
      const obs = edgeFromEvent(e);
      if (obs) {
        const a = positions.get(obs.from);
        const b = positions.get(obs.to);
        if (a && b) {
          const key = `${obs.from}->${obs.to}`;
          setEdges((prev) => (prev[key] ? prev : { ...prev, [key]: [obs.from, obs.to] }));
          if (!reduced) spawnPulse(a, b);
          return;
        }
      }
      if (!reduced) {
        const p = positions.get(known[0]);
        if (p) spawnPulse(CORE_POS, p);
      }
    });
    return off;
  }, [subscribe, positions, reduced]);

  // Activity decays toward 0 so the core calms when the swarm is quiet.
  useFrame((_s, delta) => {
    if (activity > 0) setActivityThrottled(setActivity, delta);
    // Barely-perceptible drift — the orbit autoRotate below already
    // moves the camera; both compounding read as "spinning too fast".
    if (group.current && !reduced) group.current.rotation.y += delta * 0.008;
  });

  const removePulse = (id: number) => setPulses((prev) => prev.filter((p) => p.id !== id));

  const nodeWeights = useMemo(() => {
    const w: Record<string, number> = {};
    Object.values(edges).forEach(([f, t]) => {
      w[f] = (w[f] || 0) + 1;
      w[t] = (w[t] || 0) + 1;
    });
    return w;
  }, [edges]);

  return (
    <>
      <color attach="background" args={[C.bg]} />
      <fog attach="fog" args={[C.bg, 9, 26]} />
      <ambientLight intensity={0.35} />
      <Stars radius={40} depth={30} count={1200} factor={3} saturation={0} fade speed={reduced ? 0 : 0.4} />

      <group ref={group}>
        <MemoryCore activity={activity} reduced={reduced} />

        {/* core -> each node faint tethers */}
        {seeds.map((s) => {
          const p = positions.get(s.id);
          if (!p) return null;
          return (
            <Line
              key={`tether-${s.id}`}
              points={[[0, 0, 0], [p.x, p.y, p.z]]}
              color={C.dim}
              lineWidth={0.6}
              transparent
              opacity={0.18}
            />
          );
        })}

        {/* observed agent<->agent edges */}
        {Object.entries(edges).map(([key, [f, t]]) => {
          const a = positions.get(f);
          const b = positions.get(t);
          if (!a || !b) return null;
          return (
            <Line
              key={key}
              points={[[a.x, a.y, a.z], [b.x, b.y, b.z]]}
              color={C.cyan}
              lineWidth={1}
              transparent
              opacity={0.4}
            />
          );
        })}

        {/* agent nodes — lit amber by snapshot state OR live event activity */}
        {seeds.map((s) => {
          const p = positions.get(s.id);
          if (!p) return null;
          const liveActive = (activeUntil.current[s.id] ?? 0) > nowMs;
          const busy =
            liveActive ||
            (s.state || "").toLowerCase().includes("busy") ||
            !!s.current_task;
          return (
            <AgentNode
              key={s.id}
              pos={p}
              color={busy ? C.amber : stateColor(s.state)}
              label={s.label || s.id}
              busy={busy}
              weight={nodeWeights[s.id] || 0}
              reduced={reduced}
            />
          );
        })}

        {/* traveling message pulses */}
        {pulses.map((p) => (
          <TravelingPulse key={p.id} pulse={p} onDone={removePulse} duration={0.9} />
        ))}
      </group>

      {/* Zoom disabled: the canvas swallowing wheel/pinch events made the
          page hard to scroll and trackpad pinch fell through to BROWSER
          zoom — confusing. Drag still orbits; camera framing is fixed. */}
      <OrbitControls
        enablePan={false}
        enableZoom={false}
        autoRotate={!reduced}
        autoRotateSpeed={0.12}
        rotateSpeed={0.6}
      />
    </>
  );
}

// activity decay helper kept out of the render closure
function setActivityThrottled(
  set: React.Dispatch<React.SetStateAction<number>>,
  delta: number,
) {
  set((v) => Math.max(0, v - delta * 0.4));
}

export default function CortexBrain3D({
  seeds,
  className,
}: {
  seeds: GraphNodeSeed[];
  className?: string;
}) {
  const [webgl] = useState(hasWebGL);
  const [reduced] = useState(prefersReducedMotion);

  // No WebGL — fall back to the proven 2D constellation.
  if (!webgl) return <AgentGraph seeds={seeds} className={className} />;

  return (
    <div className={className} style={{ position: "relative", minHeight: 480 }}>
      <Canvas
        camera={{ position: [0, 1.1, 8.2], fov: 50 }}
        dpr={[1, 2]}
        gl={{ antialias: true, alpha: false }}
        style={{ borderRadius: 8 }}
      >
        <Scene seeds={seeds} reduced={reduced} />
      </Canvas>
      <div
        style={{
          position: "absolute",
          left: 10,
          bottom: 8,
          display: "flex",
          gap: 12,
          fontFamily: '"JetBrains Mono", ui-monospace, monospace',
          fontSize: 10,
          letterSpacing: "0.05em",
          textTransform: "uppercase",
          color: "#566577",
          pointerEvents: "none",
        }}
      >
        <span><span style={{ color: C.cyan }}>●</span> idle</span>
        <span><span style={{ color: C.amber }}>●</span> active</span>
        <span style={{ opacity: 0.7 }}>drag to orbit</span>
      </div>
    </div>
  );
}

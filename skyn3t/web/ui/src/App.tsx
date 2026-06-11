import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, Route, Routes } from "react-router-dom";

import { api, clearAuthToken, getAuthToken, HttpError } from "./api/client";
import OverviewPage from "./routes/OverviewPage";
import AgentsPage from "./routes/AgentsPage";
import StudioPage from "./routes/StudioPage";
import CortexPage from "./routes/CortexPage";
import ActivityPage from "./routes/ActivityPage";
import ChatPage from "./routes/ChatPage";
import SkillsPage from "./routes/SkillsPage";
import KnowledgePage from "./routes/KnowledgePage";
import BuildPatternsPage from "./routes/BuildPatternsPage";
import TracesPage from "./routes/TracesPage";

export default function App() {
  const [navOpen, setNavOpen] = useState(false);

  return (
    <div className="relative z-10 min-h-screen bg-atelier">
      {/* Mobile overlay */}
      {navOpen && (
        <button
          type="button"
          aria-label="Close navigation"
          className="fixed inset-0 z-40 bg-bg-0/70 backdrop-blur-sm lg:hidden"
          onClick={() => setNavOpen(false)}
        />
      )}

      <div className="grid lg:grid-cols-[260px_minmax(0,1fr)] min-h-screen">
        <Sidebar open={navOpen} onClose={() => setNavOpen(false)} />

        <div className="flex flex-col min-w-0 min-h-screen">
          <MobileTopBar onMenu={() => setNavOpen(true)} />
          <main className="flex-1 min-w-0 p-4 sm:p-6 space-y-4 page-enter">
            <BackendStatusBanner />
            <Routes>
              <Route path="/" element={<OverviewPage />} />
              <Route path="/agents" element={<AgentsPage />} />
              <Route path="/studio" element={<StudioPage />} />
              <Route path="/cortex" element={<CortexPage />} />
              <Route path="/activity" element={<ActivityPage />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/skills" element={<SkillsPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="/build-patterns" element={<BuildPatternsPage />} />
              <Route path="/traces" element={<TracesPage />} />
            </Routes>
          </main>
        </div>
      </div>
    </div>
  );
}

function MobileTopBar({ onMenu }: { onMenu: () => void }) {
  return (
    <header className="lg:hidden sticky top-0 z-30 flex items-center gap-3 px-4 py-3 border-b border-border bg-bg-1/95 backdrop-blur-md">
      <button
        type="button"
        onClick={onMenu}
        className="w-9 h-9 rounded border border-border flex items-center justify-center text-text-secondary hover:text-accent hover:border-accent-line transition-colors"
        aria-label="Open navigation"
      >
        <i className="fa-solid fa-bars" />
      </button>
      <div className="display text-lg wordmark-glow">
        <span className="text-accent">SKYN3</span>
        <span className="text-chrome-bright">T</span>
      </div>
      <span className="ml-auto text-[0.6rem] uppercase tracking-[0.16em] text-text-dim">
        Mission Control
      </span>
    </header>
  );
}

function BackendStatusBanner() {
  const token = getAuthToken();
  const [input, setInput] = useState("");
  const { error, isFetching } = useQuery({
    queryKey: ["auth_probe"],
    queryFn: api.status,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: 5_000,
  });

  if (!error) return null;

  const isAuth = error instanceof HttpError && error.status === 401;
  const isServerDown =
    !isAuth &&
    (error instanceof TypeError ||
      (error instanceof HttpError && error.status >= 500));

  if (isAuth) {
    return (
      <div className="rounded-lg border border-status-yellow/40 bg-status-yellow/10 p-4">
        <div className="text-sm font-medium text-status-yellow mb-1">
          <i className="fa-solid fa-lock mr-2" />
          Backend requires an auth token
        </div>
        <p className="text-xs text-text-secondary mb-3">
          The backend is running with{" "}
          <code className="bg-bg-3 px-1 rounded font-mono">SKYN3T_WEB_TOKEN</code> set.
          Paste it below — stored in localStorage and sent as{" "}
          <code className="bg-bg-3 px-1 rounded font-mono">Authorization: Bearer …</code>.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (input.trim()) {
              try {
                localStorage.setItem("skyn3t_token", input.trim());
              } catch {
                /* */
              }
              window.location.reload();
            }
          }}
          className="flex gap-2 items-center flex-wrap"
        >
          <input
            type="password"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="paste token"
            className="flex-1 min-w-[12rem] data-input font-mono"
          />
          <button type="submit" className="btn-primary">
            Save & reload
          </button>
          {token && (
            <button
              type="button"
              onClick={() => {
                clearAuthToken();
                window.location.reload();
              }}
              className="text-xs text-text-dim hover:text-text-primary"
            >
              clear
            </button>
          )}
        </form>
      </div>
    );
  }

  if (isServerDown) {
    return (
      <div className="rounded-lg border border-status-red/40 bg-status-red/10 p-4">
        <div className="flex items-center gap-2 text-sm font-medium text-status-red mb-1">
          <i className="fa-solid fa-triangle-exclamation" />
          Backend is unreachable
          {isFetching && (
            <span className="text-[0.65rem] text-text-dim font-mono uppercase tracking-wider ml-auto">
              <i className="fa-solid fa-arrows-rotate animate-spin mr-1" />
              retrying
            </span>
          )}
        </div>
        <p className="text-xs text-text-secondary">
          The orchestrator on{" "}
          <code className="bg-bg-3 px-1 rounded font-mono">127.0.0.1:6660</code> isn't
          responding. Restart it with:
        </p>
        <pre className="text-xs font-mono bg-bg-3 border border-border rounded p-2 mt-2 overflow-x-auto">
          bash scripts/restart-backend.sh
        </pre>
      </div>
    );
  }

  return null;
}

function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const items = [
    { to: "/", label: "Overview", icon: "fa-solid fa-gauge-high" },
    { to: "/agents", label: "Agents", icon: "fa-solid fa-robot" },
    { to: "/studio", label: "Studio", icon: "fa-solid fa-hammer" },
    { to: "/cortex", label: "Cortex", icon: "fa-solid fa-brain" },
    { to: "/activity", label: "Activity", icon: "fa-solid fa-circle-nodes" },
    { to: "/chat", label: "Chat", icon: "fa-solid fa-comments" },
    { to: "/skills", label: "Skills", icon: "fa-solid fa-graduation-cap" },
    { to: "/knowledge", label: "Knowledge", icon: "fa-solid fa-book" },
    { to: "/build-patterns", label: "Build Patterns", icon: "fa-solid fa-route" },
    { to: "/traces", label: "Traces", icon: "fa-solid fa-stethoscope" },
  ];

  return (
    <aside
      className={[
        "fixed lg:sticky top-0 z-50 h-screen w-[260px] flex flex-col gap-5 border-r border-border px-5 py-6 transition-transform duration-200",
        "bg-bg-1/98 backdrop-blur-md lg:translate-x-0",
        open ? "translate-x-0" : "-translate-x-full",
      ].join(" ")}
      style={{
        background:
          "radial-gradient(ellipse 200px 120px at 20% 0%, rgba(56,212,240,0.08), transparent 70%), linear-gradient(180deg, #111520 0%, #0c0e12 100%)",
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="display text-xl tracking-wide wordmark-glow">
            <span className="text-accent">SKYN3</span>
            <span className="text-chrome-bright">T</span>
          </div>
          <div className="text-[0.6rem] tracking-[0.18em] text-text-dim uppercase mt-1">
            ChoateLabs · Autonomous AI
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="lg:hidden w-8 h-8 rounded border border-border text-text-dim hover:text-text-primary"
          aria-label="Close navigation"
        >
          <i className="fa-solid fa-xmark" />
        </button>
      </div>

      <nav className="flex flex-col gap-0.5 text-sm flex-1 overflow-y-auto">
        <div className="section-label px-3 mb-2">Navigation</div>
        {items.map((it) => (
          <NavLink
            key={it.to}
            to={it.to}
            end={it.to === "/"}
            onClick={onClose}
            className={({ isActive }) =>
              [
                "relative flex items-center gap-3 rounded-md px-3 py-2 transition-colors",
                isActive
                  ? "bg-accent-soft text-accent font-medium border border-accent-line"
                  : "text-text-secondary hover:bg-accent-soft/60 hover:text-text-primary border border-transparent",
              ].join(" ")
            }
          >
            <i className={[it.icon, "w-4 text-center text-xs opacity-80"].join(" ")} />
            <span>{it.label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="text-[0.6rem] text-text-dim font-mono border-t border-border pt-4">
        <span className="live-dot mr-1.5 align-middle" />
        polling · 127.0.0.1:6660
      </div>
    </aside>
  );
}

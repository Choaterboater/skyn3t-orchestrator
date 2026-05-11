import { NavLink, Route, Routes } from "react-router-dom";

import OverviewPage from "./routes/OverviewPage";
import AgentsPage from "./routes/AgentsPage";
import ChatPage from "./routes/ChatPage";
import SkillsPage from "./routes/SkillsPage";
import BuildPatternsPage from "./routes/BuildPatternsPage";

// Layout: sidebar + main. The old dashboard.html shoved everything
// into a single page with hidden divs; here every route is a real
// component with its own data dependencies.
export default function App() {
  return (
    <div className="relative z-10 grid grid-cols-[260px_minmax(0,1fr)] min-h-screen bg-atelier">
      <Sidebar />
      <main className="min-w-0 p-6">
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/build-patterns" element={<BuildPatternsPage />} />
        </Routes>
      </main>
    </div>
  );
}

function Sidebar() {
  // Single source of truth for the nav. Add a route here once it's
  // wired in App's <Routes>.
  const items = [
    { to: "/",               label: "Overview",        icon: "fa-solid fa-gauge" },
    { to: "/agents",         label: "Agents",          icon: "fa-solid fa-robot" },
    { to: "/chat",           label: "Chat",            icon: "fa-solid fa-comments" },
    { to: "/skills",         label: "Skills",          icon: "fa-solid fa-graduation-cap" },
    { to: "/build-patterns", label: "Build Patterns",  icon: "fa-solid fa-route" },
  ];
  return (
    <aside className="flex flex-col gap-6 border-r border-border bg-bg-1 px-5 py-6">
      <div className="display text-2xl">
        <span className="text-accent">S</span>kyN3t
      </div>
      <nav className="flex flex-col gap-1 text-sm">
        {items.map((it) => (
          <NavLink
            key={it.to}
            to={it.to}
            end={it.to === "/"}
            className={({ isActive }) =>
              [
                "relative flex items-center gap-3 rounded-md px-3 py-2 transition",
                isActive
                  ? "bg-accent-soft text-accent-strong font-semibold border border-accent-line"
                  : "text-text-secondary hover:bg-accent-soft hover:text-text-primary",
              ].join(" ")
            }
          >
            <i className={it.icon} />
            <span>{it.label}</span>
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}

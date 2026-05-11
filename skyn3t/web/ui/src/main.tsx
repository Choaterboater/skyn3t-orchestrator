import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { HttpError } from "./api/client";
import App from "./App";
import "./styles/globals.css";

// React Query handles every /api/* fetch — caching, refetching, polling,
// loading states. Defaults tuned for a dashboard: 5s stale time so a
// view that re-mounts mid-poll doesn't refire instantly, and refetch on
// window focus so reopening a tab shows fresh data.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: true,
      // Don't retry 4xx (auth, not found, bad request) — they won't
      // recover with another attempt. Do retry 5xx once: a transient
      // backend restart often clears by then.
      retry: (failureCount, error) => {
        if (error instanceof HttpError) {
          if (error.status >= 400 && error.status < 500) return false;
        }
        return failureCount < 1;
      },
      retryDelay: (attempt) => Math.min(15_000, 1000 * 2 ** attempt),
    },
    mutations: {
      retry: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);

import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
  /**
   * Optional name surfaced in the rendered fallback. Useful when
   * nesting boundaries around individual route panels — e.g. a
   * "StudioPage failed" message is more actionable than "an error
   * occurred."
   */
  scope?: string;
}

interface ErrorBoundaryState {
  error: Error | null;
  info: ErrorInfo | null;
}

/**
 * Top-level error boundary.
 *
 * Without this, a single thrown error anywhere in the tree (e.g. a
 * route component that fails to mount because of a stale API contract)
 * unmounts everything and leaves a blank `<div id="root">`. The user
 * sees a white page with no signal about what went wrong.
 *
 * This boundary renders a readable diagnostic instead, including the
 * error message, stack, and a refresh button. Errors are also logged
 * to the browser console so they show up in DevTools.
 */
export default class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null, info: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error, info: null };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Console-log so DevTools shows the full trace.
    // eslint-disable-next-line no-console
    console.error("[SkyN3t] React render error:", error, info);
    this.setState({ error, info });
  }

  reset = (): void => {
    this.setState({ error: null, info: null });
  };

  reload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (!this.state.error) {
      return this.props.children;
    }
    const scope = this.props.scope ? ` (${this.props.scope})` : "";
    const message = this.state.error.message || String(this.state.error);
    const stack = this.state.error.stack ?? "";
    const componentStack = this.state.info?.componentStack ?? "";

    return (
      <div className="min-h-screen bg-bg-0 p-6">
        <div className="max-w-3xl mx-auto space-y-4">
          <header className="border-b border-border pb-3">
            <h1 className="display text-xl text-accent">
              Dashboard error{scope}
            </h1>
            <p className="text-text-secondary text-sm mt-1">
              Something threw while rendering. The error below is the actual
              exception — fix that and refresh.
            </p>
          </header>

          <section className="rounded-md border border-status-red/40 bg-status-red/10 p-4">
            <p className="font-mono text-sm text-text-primary break-words">
              {message}
            </p>
          </section>

          {stack && (
            <details className="rounded-md border border-border bg-bg-1 p-3">
              <summary className="cursor-pointer text-text-secondary text-sm">
                Stack trace
              </summary>
              <pre className="mt-2 overflow-x-auto text-xs text-text-secondary whitespace-pre-wrap">
                {stack}
              </pre>
            </details>
          )}

          {componentStack && (
            <details className="rounded-md border border-border bg-bg-1 p-3">
              <summary className="cursor-pointer text-text-secondary text-sm">
                Component stack
              </summary>
              <pre className="mt-2 overflow-x-auto text-xs text-text-secondary whitespace-pre-wrap">
                {componentStack}
              </pre>
            </details>
          )}

          <div className="flex gap-2 pt-2">
            <button
              onClick={this.reset}
              className="rounded bg-accent-soft border border-accent-line text-accent px-3 py-1.5 text-sm hover:bg-accent/20"
            >
              Try again
            </button>
            <button
              onClick={this.reload}
              className="rounded bg-bg-2 border border-border text-text-primary px-3 py-1.5 text-sm hover:bg-bg-3"
            >
              Reload page
            </button>
          </div>
        </div>
      </div>
    );
  }
}

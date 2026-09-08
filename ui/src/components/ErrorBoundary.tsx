import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Unhandled render error:", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="flex min-h-screen items-center justify-center bg-bg p-6">
        <div className="w-full max-w-xl rounded-lg border border-line bg-card p-6">
          <h1 className="text-[15px] font-semibold text-ink-h">This screen failed to render</h1>
          <p className="mt-2 text-[12.5px] leading-relaxed text-ink-2">
            An unexpected error escaped the page. The details below are the fastest route
            to the cause; the rest of the app is still reachable from the sidebar.
          </p>
          <pre className="mt-4 max-h-64 overflow-auto rounded-md border border-line bg-input p-3 font-mono text-[11.5px] leading-relaxed text-crit">
            {error.message}
          </pre>
          <div className="mt-4 flex gap-2">
            <button
              type="button"
              onClick={() => this.setState({ error: null })}
              className="rounded-md border border-accent-border bg-accent-bg px-3 py-1.5 text-[12px] font-medium text-accent transition-colors hover:bg-accent hover:text-bg"
            >
              Try again
            </button>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="rounded-md border border-line bg-input px-3 py-1.5 text-[12px] font-medium text-ink-2 transition-colors hover:text-ink"
            >
              Reload
            </button>
          </div>
        </div>
      </div>
    );
  }
}

import { Component, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error?: Error;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info);
  }

  handleReset = () => {
    this.setState({ hasError: false, error: undefined });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center h-full text-center py-20 px-6">
          <div className="w-14 h-14 rounded-2xl bg-accent-red/10 flex items-center justify-center mb-4">
            <AlertTriangle size={24} className="text-accent-red" />
          </div>
          <h2 className="text-sm font-semibold text-ink mb-1">
            页面渲染出错
          </h2>
          <p className="text-xs text-ink-tertiary max-w-sm mb-4">
            组件渲染过程中发生了意外错误。这可能是由数据异常或代码缺陷引起的。
          </p>
          {this.state.error && (
            <pre className="text-[11px] text-accent-red/70 bg-accent-red/[0.03] border border-accent-red/10 rounded-xl p-3 max-w-lg w-full text-left overflow-auto mb-4">
              {this.state.error.message}
            </pre>
          )}
          <button
            onClick={this.handleReset}
            className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-medium bg-accent-blue text-white hover:bg-accent-blue-dark transition-all"
          >
            <RotateCcw size={13} />
            重试
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

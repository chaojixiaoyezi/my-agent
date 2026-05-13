import { X, CheckCircle, AlertCircle, Info } from "lucide-react";
import { cn } from "../../lib/utils";

export function Toast({
  message,
  type = "info",
  onClose,
}: {
  message: string;
  type?: "success" | "error" | "info";
  onClose?: () => void;
}) {
  const icons = {
    success: CheckCircle,
    error: AlertCircle,
    info: Info,
  };
  const colors = {
    success: "text-accent-green bg-accent-green/10 border-accent-green/20",
    error: "text-accent-red bg-accent-red/10 border-accent-red/20",
    info: "text-accent-blue bg-accent-blue/10 border-accent-blue/20",
  };
  const Icon = icons[type];

  return (
    <div
      className={cn(
        "fixed top-4 left-1/2 -translate-x-1/2 z-50 flex items-center gap-2.5 px-4 py-2.5 rounded-2xl border text-sm font-medium shadow-card",
        "animate-in slide-in-from-top-2 fade-in duration-250 transition-bounce",
        colors[type]
      )}
    >
      <Icon size={16} />
      <span>{message}</span>
      {onClose && (
        <button onClick={onClose} className="ml-1 hover:opacity-70">
          <X size={14} />
        </button>
      )}
    </div>
  );
}

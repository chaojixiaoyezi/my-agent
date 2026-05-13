import { useAuthStore } from "../../stores/authStore";
import { cn } from "../../lib/utils";
import { Lock } from "lucide-react";

export function PermissionGate({
  requireAdmin = false,
  children,
  fallback,
}: {
  requireAdmin?: boolean;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}) {
  const isAdmin = useAuthStore((s) => s.isAdmin);

  if (requireAdmin && !isAdmin) {
    if (fallback) return <>{fallback}</>;
    return (
      <div className="flex items-center gap-2 rounded-xl border border-border bg-black/[0.02] px-4 py-6 text-sm text-ink-tertiary">
        <Lock size={14} />
        <span>需要管理员权限</span>
      </div>
    );
  }

  return <>{children}</>;
}

export function ReadOnlyMask({
  visible,
  children,
  className,
}: {
  visible: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("relative", className)}>
      {children}
      {visible && (
        <div className="absolute inset-0 z-10 flex items-center justify-center rounded-xl bg-white/60 backdrop-blur-[1px]">
          <span className="flex items-center gap-1.5 text-xs font-medium text-ink-tertiary">
            <Lock size={12} /> 只读
          </span>
        </div>
      )}
    </div>
  );
}

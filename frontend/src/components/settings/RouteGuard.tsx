import { useEffect } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useAuthStore } from "../../stores/authStore";

export function RouteGuard({ children }: { children: React.ReactNode }) {
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const canAccessPage = useAuthStore((s) => s.canAccessPage);
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    if (!canAccessPage(location.pathname)) {
      navigate("/", { replace: true });
    }
  }, [location.pathname, canAccessPage, navigate]);

  // Also block rendering if not admin on admin-only pages
  if (!isAdmin && location.pathname.startsWith("/settings")) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center py-20">
        <div className="w-16 h-16 rounded-2xl bg-accent-red/10 flex items-center justify-center mb-4">
          <span className="text-2xl">🔒</span>
        </div>
        <h2 className="text-sm font-semibold text-ink mb-1">权限不足</h2>
        <p className="text-xs text-ink-tertiary max-w-xs">
          当前用户角色无法访问此页面。请联系管理员获取权限。
        </p>
      </div>
    );
  }

  return <>{children}</>;
}

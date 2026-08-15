import { Wrench, Shield, Gauge } from "lucide-react";
import { cn } from "../lib/utils";
import {
  frontendRuntimeConfig,
  runtimeRoleToolPermissions,
  runtimeTools,
} from "../data/runtimeConfig";

export default function Tools() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-xl bg-accent-blue flex items-center justify-center">
          <Wrench size={16} className="text-white" />
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink">工具列表</h2>
          <p className="text-[11px] text-ink-tertiary">共 {runtimeTools.length} 个已启用工具</p>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-3">
        {runtimeTools.map((t) => (
          <div
            key={t.name}
            className="bg-card rounded-2xl border border-border p-4 shadow-card hover:shadow-card-hover hover:-translate-y-0.5 transition-all duration-200"
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium text-ink">{t.name}</span>
              <span
                className={cn(
                  "w-2 h-2 rounded-full",
                  t.enabled ? "bg-accent-green" : "bg-ink-tertiary"
                )}
              />
            </div>
            <div className="text-xs text-ink-secondary mb-2">{t.desc}</div>
            <div className="text-[11px] text-ink-tertiary">
              调用 {t.calls.toLocaleString()} 次
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-xl bg-accent-orange flex items-center justify-center">
          <Shield size={16} className="text-white" />
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink">工具权限矩阵</h2>
          <p className="text-[11px] text-ink-tertiary">各角色可使用的工具</p>
        </div>
      </div>

      <div className="bg-card rounded-2xl border border-border shadow-card overflow-hidden">
        <div
          className="divide-x divide-border border-b border-border bg-black/[0.01]"
          style={{ display: "grid", gridTemplateColumns: `140px repeat(${runtimeTools.length}, minmax(84px, 1fr))` }}
        >
          <div className="px-3 py-2 text-[11px] font-medium text-ink">角色</div>
          {runtimeTools.map((t) => (
            <div
              key={t.name}
              className="px-2 py-2 text-[10px] font-medium text-ink-secondary text-center truncate"
              title={t.name}
            >
              {t.name}
            </div>
          ))}
        </div>
        {runtimeRoleToolPermissions.map((p) => (
          <div
            key={p.role}
            className="divide-x divide-border border-b border-border last:border-b-0"
            style={{ display: "grid", gridTemplateColumns: `140px repeat(${runtimeTools.length}, minmax(84px, 1fr))` }}
          >
            <div className="px-3 py-2.5 text-xs font-medium text-ink flex items-center">
              {p.role}
            </div>
            {runtimeTools.map((t) => (
              <div
                key={t.name}
                className="px-2 py-2.5 flex items-center justify-center"
              >
                {p.tools.includes(t.name) ? (
                  <div className="w-4 h-4 rounded-md bg-accent-green/10 flex items-center justify-center">
                    <div className="w-1.5 h-1.5 rounded-full bg-accent-green" />
                  </div>
                ) : (
                  <div className="w-4 h-4 rounded-md bg-black/[0.03]" />
                )}
              </div>
            ))}
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-xl bg-accent-purple flex items-center justify-center">
          <Gauge size={16} className="text-white" />
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink">工具预算</h2>
          <p className="text-[11px] text-ink-tertiary">当前周期内调用统计</p>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <div className="bg-card rounded-2xl border border-border p-4 shadow-card">
          <div className="text-xs text-ink-secondary mb-1">工具预算窗口</div>
          <div className="text-lg font-semibold text-ink">
            {frontendRuntimeConfig.tools.budget.window_seconds} 秒
          </div>
          <div className="text-[11px] text-ink-tertiary">每 10 分钟重置</div>
        </div>
        <div className="bg-card rounded-2xl border border-border p-4 shadow-card">
          <div className="text-xs text-ink-secondary mb-1">最大调用次数</div>
          <div className="text-lg font-semibold text-ink">
            {frontendRuntimeConfig.tools.budget.max_calls} 次
          </div>
          <div className="text-[11px] text-ink-tertiary">
            当前 12 / {frontendRuntimeConfig.tools.budget.max_calls}
          </div>
        </div>
        <div className="bg-card rounded-2xl border border-border p-4 shadow-card">
          <div className="text-xs text-ink-secondary mb-1">Controlled Exec</div>
          <div className="text-lg font-semibold text-accent-green">已启用</div>
          <div className="text-[11px] text-ink-tertiary">白名单模式</div>
        </div>
      </div>
    </div>
  );
}

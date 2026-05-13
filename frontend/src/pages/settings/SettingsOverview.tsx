import { useAuthStore } from "../../stores/authStore";
import { useSettingsStore } from "../../stores/settingsStore";
import { useConfigStore } from "../../stores/configStore";
import { PermissionGate } from "../../components/settings/PermissionGate";
import { cn } from "../../lib/utils";
import {
  Save,
  Loader2,
  Users,
  Activity,
  Brain,
  Wrench,
  Database,
  Server,
  Shield,
  Puzzle,
  ArrowRight,
} from "lucide-react";
import { Link } from "react-router-dom";

const cards = [
  { to: "/settings/dispatch", title: "调度参数", subtitle: "Dispatch Loop / Runner / Daemon", icon: Activity },
  { to: "/settings/model", title: "模型参数", subtitle: "Model / Sampling / Backend", icon: Brain },
  { to: "/settings/tools", title: "工具参数", subtitle: "Tool Budget / Limits / Registry", icon: Wrench },
  { to: "/settings/memory", title: "记忆参数", subtitle: "Memory / Archive / Routing", icon: Database },
  { to: "/settings/gateway", title: "网关参数", subtitle: "Gateway / Watchdog / Lease", icon: Server },
  { to: "/settings/subagents", title: "子代理参数", subtitle: "Subagent / Workflow / Acceptance", icon: Users },
  { to: "/settings/security", title: "安全策略", subtitle: "Security / Audit / Local Store", icon: Shield },
  { to: "/settings/adapters", title: "适配器", subtitle: "Feishu / QQ / Workspace", icon: Puzzle },
];

export default function SettingsOverview() {
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const saving = useSettingsStore((s) => s.saving);
  const setSaving = useSettingsStore((s) => s.setSaving);
  const login = useAuthStore((s) => s.login);
  const showToast = useConfigStore((s) => s.showToast);

  const handleSave = async () => {
    setSaving(true);
    await new Promise((r) => setTimeout(r, 800));
    setSaving(false);
    showToast("系统设置已保存（mock）", "success");
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-ink">系统设置（System Settings）</h2>
          <p className="text-[11px] text-ink-tertiary mt-0.5">
            所有系统级参数按功能分类，点击下方卡片进入对应子页面
          </p>
        </div>
        <PermissionGate requireAdmin>
          <button
            onClick={handleSave}
            disabled={saving}
            className={cn(
              "flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-medium text-white transition-all duration-150",
              saving
                ? "bg-accent-blue/40 cursor-not-allowed"
                : "bg-accent-blue hover:bg-accent-blue-dark shadow-card hover:shadow-card-hover hover:-translate-y-px"
            )}
          >
            {saving ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                保存中...
              </>
            ) : (
              <>
                <Save size={13} />
                保存全部
              </>
            )}
          </button>
        </PermissionGate>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {cards.map((card) => {
          const Icon = card.icon;
          return (
            <Link
              key={card.to}
              to={card.to}
              className="group flex items-center gap-3 p-4 bg-card rounded-2xl border border-border shadow-card hover:border-border-hover hover:shadow-card-hover transition-all duration-150"
            >
              <div className="w-10 h-10 rounded-xl bg-accent-blue/10 flex items-center justify-center shrink-0">
                <Icon size={18} className="text-accent-blue" />
              </div>
              <div className="flex-1 min-w-0">
                <h3 className="text-sm font-medium text-ink">{card.title}</h3>
                <p className="text-[11px] text-ink-tertiary truncate">{card.subtitle}</p>
              </div>
              <ArrowRight
                size={14}
                className="text-ink-tertiary group-hover:text-accent-blue transition-colors shrink-0"
              />
            </Link>
          );
        })}
      </div>

      {/* Auth Simulation */}
      <section className="bg-card rounded-2xl border border-border p-5 shadow-card">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-8 h-8 rounded-xl bg-accent-blue/10 flex items-center justify-center">
            <Users size={16} className="text-accent-blue" />
          </div>
          <div>
            <h3 className="text-sm font-semibold text-ink">权限模拟（Auth Simulation）</h3>
            <p className="text-[11px] text-ink-tertiary">切换角色以测试不同权限下的界面表现</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={() => login("admin")}
            className={cn(
              "px-4 py-2 rounded-xl text-xs font-medium border transition-all",
              isAdmin
                ? "bg-accent-blue/10 border-accent-blue/20 text-accent-blue"
                : "bg-card border-border text-ink-secondary hover:border-border-hover"
            )}
          >
            管理员 (Admin)
          </button>
          <button
            onClick={() => login("user")}
            className={cn(
              "px-4 py-2 rounded-xl text-xs font-medium border transition-all",
              !isAdmin
                ? "bg-accent-blue/10 border-accent-blue/20 text-accent-blue"
                : "bg-card border-border text-ink-secondary hover:border-border-hover"
            )}
          >
            普通用户 (User)
          </button>
        </div>
        <p className="text-[11px] text-ink-tertiary mt-2">
          当前角色：<strong className="text-ink">{isAdmin ? "管理员 (Admin)" : "普通用户 (User)"}</strong>
          {isAdmin
            ? " — 可编辑所有系统配置"
            : " — 仅可查看，不可修改任何配置项"}
        </p>
      </section>
    </div>
  );
}

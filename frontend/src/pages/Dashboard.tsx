import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Activity,
  Server,
  Brain,
  Users,
  Database,
  Bell,
  Zap,
  AlertCircle,
  Loader2,
} from "lucide-react";
import { cn } from "../lib/utils";
import { useConfigStore } from "../stores/configStore";
import { restartGateway, runSmokeTest } from "../api/mockApi";

function StatusCard({
  title,
  value,
  subtitle,
  icon: Icon,
  color,
}: {
  title: string;
  value: string;
  subtitle?: string;
  icon: React.ElementType;
  color: string;
}) {
  return (
    <div className="bg-card rounded-2xl border border-border p-4 shadow-card hover:shadow-card-hover hover:-translate-y-0.5 transition-all duration-200">
      <div className="flex items-center justify-between mb-3">
        <div className={cn("w-8 h-8 rounded-xl flex items-center justify-center", color)}>
          <Icon size={16} className="text-white" />
        </div>
      </div>
      <div className="text-lg font-semibold text-ink">{value}</div>
      <div className="text-xs text-ink-secondary mt-0.5">{title}</div>
      {subtitle && (
        <div className="text-[11px] text-ink-tertiary mt-1">{subtitle}</div>
      )}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="text-sm font-semibold text-ink mb-3">{children}</h2>;
}

function QuickActionButton({
  onClick,
  children,
  isLoading,
}: {
  onClick: () => void;
  children: React.ReactNode;
  isLoading?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={isLoading}
      className={cn(
        "px-4 py-2 rounded-xl text-xs font-medium bg-card border border-border text-ink-secondary hover:border-border-hover hover:-translate-y-0.5 hover:shadow-card-hover transition-all duration-150",
        isLoading && "opacity-60 cursor-not-allowed"
      )}
    >
      {isLoading ? (
        <span className="flex items-center gap-1.5">
          <Loader2 size={12} className="animate-spin" />
          处理中...
        </span>
      ) : (
        children
      )}
    </button>
  );
}

export default function Dashboard() {
  const navigate = useNavigate();
  const showToast = useConfigStore((s) => s.showToast);
  const [restarting, setRestarting] = useState(false);
  const [testing, setTesting] = useState(false);

  const handleRestartGateway = async () => {
    setRestarting(true);
    try {
      const res = await restartGateway();
      showToast(res.message, "success");
    } catch {
      showToast("重启失败（mock）", "error");
    } finally {
      setRestarting(false);
    }
  };

  const handleSmokeTest = async () => {
    setTesting(true);
    try {
      const res = await runSmokeTest();
      showToast(res.message, "success");
    } catch {
      showToast("Smoke Test 失败（mock）", "error");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="space-y-6">
      <SectionTitle>系统状态</SectionTitle>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatusCard
          title="Gateway"
          value="运行中"
          subtitle="PID 12345 · 已运行 1h 12m"
          icon={Server}
          color="bg-accent-green"
        />
        <StatusCard
          title="Daemon"
          value="已停止"
          subtitle="上次运行 2h 前"
          icon={Activity}
          color="bg-ink-tertiary"
        />
        <StatusCard
          title="模型后端"
          value="MiniMax-M2.7"
          subtitle="Anthropic 兼容 · 延迟 1.2s"
          icon={Brain}
          color="bg-accent-blue"
        />
        <StatusCard
          title="活跃子代理"
          value="5"
          subtitle="3 待处理 · 2 待验收"
          icon={Users}
          color="bg-accent-orange"
        />
      </div>

      <SectionTitle>资源概览</SectionTitle>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatusCard
          title="记忆记录"
          value="12,450"
          subtitle="+156 今日"
          icon={Database}
          color="bg-accent-purple"
        />
        <StatusCard
          title="待处理请求"
          value="2"
          subtitle="1 处理中 · 0 失败"
          icon={Zap}
          color="bg-accent-cyan"
        />
        <StatusCard
          title="未读通知"
          value="2"
          subtitle="1 警告 · 1 信息"
          icon={Bell}
          color="bg-accent-orange"
        />
        <StatusCard
          title="最近错误"
          value="0"
          subtitle="过去 24h"
          icon={AlertCircle}
          color="bg-accent-green"
        />
      </div>

      <SectionTitle>子代理红绿灯</SectionTitle>
      <div className="bg-card rounded-2xl border border-border p-4 shadow-card">
        <div className="flex items-center gap-4">
          {[
            { label: "运行中", count: 2, color: "bg-accent-green" },
            { label: "待处理", count: 3, color: "bg-accent-orange" },
            { label: "已完成", count: 8, color: "bg-ink-tertiary" },
            { label: "阻塞", count: 0, color: "bg-accent-red" },
          ].map((s) => (
            <div key={s.label} className="flex items-center gap-2">
              <span className={cn("w-2.5 h-2.5 rounded-full", s.color)} />
              <span className="text-sm text-ink-secondary">{s.label}</span>
              <span className="text-sm font-medium text-ink">{s.count}</span>
            </div>
          ))}
        </div>
      </div>

      <SectionTitle>最近事件</SectionTitle>
      <div className="bg-card rounded-2xl border border-border p-4 shadow-card space-y-2">
        {[
          { time: "10:15", msg: "子代理 worker-001 完成 write_file 工具调用", type: "info" },
          { time: "10:12", msg: "Gateway worker-1 完成 run_001", type: "success" },
          { time: "10:08", msg: "子代理 coordinator-001 创建 child 任务", type: "info" },
          { time: "09:55", msg: "工具调用超时：run_command 超过 30s", type: "warn" },
          { time: "09:30", msg: "Gateway 启动成功，监听端口 8420", type: "success" },
        ].map((e, i) => (
          <div key={i} className="flex items-start gap-3 text-sm">
            <span className="text-ink-tertiary text-xs shrink-0 w-10">{e.time}</span>
            <span
              className={cn(
                "w-1.5 h-1.5 rounded-full mt-1.5 shrink-0",
                e.type === "success"
                  ? "bg-accent-green"
                  : e.type === "warn"
                  ? "bg-accent-orange"
                  : "bg-accent-blue"
              )}
            />
            <span className="text-ink-secondary">{e.msg}</span>
          </div>
        ))}
      </div>

      <SectionTitle>快捷操作</SectionTitle>
      <div className="flex gap-2">
        <QuickActionButton onClick={handleRestartGateway} isLoading={restarting}>
          重启 Gateway
        </QuickActionButton>
        <QuickActionButton onClick={handleSmokeTest} isLoading={testing}>
          运行 Smoke Test
        </QuickActionButton>
        <QuickActionButton onClick={() => navigate("/config")}>
          打开配置页
        </QuickActionButton>
        <QuickActionButton onClick={() => navigate("/logs")}>
          查看日志
        </QuickActionButton>
      </div>
    </div>
  );
}

import { useSettingsStore } from "../../stores/settingsStore";
import { useAuthStore } from "../../stores/authStore";
import {
  AdminSection,
  NumberField,
  StringField,
  ChoiceField,
  ToggleField,
  Divider,
} from "../../components/settings/SettingsFieldComponents";
import { SettingsPageHeader } from "../../components/settings/SettingsPageHeader";
import { useSettingsSection } from "../../components/settings/useSettingsSection";
import { useDirtyGuard } from "../../components/settings/useDirtyGuard";
import { Activity, Server } from "lucide-react";

export default function SettingsDispatch() {
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const dispatch = useSettingsStore((s) => s.dispatchParams);
  const setDispatch = useSettingsStore((s) => s.setDispatchParams);
  const runner = useSettingsStore((s) => s.runnerParams);
  const setRunner = useSettingsStore((s) => s.setRunnerParams);
  const daemon = useSettingsStore((s) => s.daemonParams);
  const setDaemon = useSettingsStore((s) => s.setDaemonParams);
  const errors = useSettingsStore((s) => s.errors);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("调度参数");
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="调度参数（Dispatch）" subtitle="Dispatch Loop、Runner 调度策略和 Daemon 后台任务参数" saving={saving} onSave={handleSave} dirty={dirty} />

      {/* Dispatch Loop */}
      <AdminSection
        icon={Activity}
        title="调度循环（Dispatch Loop）"
        subtitle="控制主代理调度循环的并发和轮次策略"
      >
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
          <NumberField
            label="max_consecutive_rounds（最大连续轮数）"
            description="单次 dispatch_loop 最多执行多少轮"
            value={dispatch.max_consecutive_rounds}
            onChange={(v) => { setDispatch({ max_consecutive_rounds: v }); markDirty(); }}
            min={1}
            max={100}
            unit="轮"
            disabled={!isAdmin}
            error={errors["dp_rounds"]}
          />
          <NumberField
            label="max_runners（最大并发执行器）"
            description="单轮调度中同时启动的最大 runner 数量"
            value={dispatch.max_runners}
            onChange={(v) => { setDispatch({ max_runners: v }); markDirty(); }}
            min={1}
            max={20}
            unit="个"
            disabled={!isAdmin}
          />
          <NumberField
            label="limit（单次调度上限）"
            description="单轮 dispatch 中处理的候选上限"
            value={dispatch.limit}
            onChange={(v) => { setDispatch({ limit: v }); markDirty(); }}
            min={1}
            max={100}
            unit="个"
            disabled={!isAdmin}
          />
          <NumberField
            label="dispatch_active_interval（活跃调度间隔）"
            description="调度器活跃状态下的轮询间隔"
            value={dispatch.active_interval}
            onChange={(v) => { setDispatch({ active_interval: v }); markDirty(); }}
            min={1}
            max={60}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="dispatch_idle_interval（空闲调度间隔）"
            description="调度器空闲状态下的轮询间隔"
            value={dispatch.idle_interval}
            onChange={(v) => { setDispatch({ idle_interval: v }); markDirty(); }}
            min={1}
            max={300}
            unit="秒"
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* Runner / Scheduler */}
      <AdminSection
        icon={Activity}
        title="Runner / Scheduler"
        subtitle="执行器并发控制与调度策略"
      >
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
          <ChoiceField
            label="scheduler_mode（调度器模式）"
            description="runner 的调度策略"
            value={runner.scheduler_mode}
            choices={["priority", "round-robin", "fifo", "adaptive"]}
            onChange={(v) => { setRunner({ scheduler_mode: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <NumberField
            label="runner_concurrency（并发数）"
            description="同时运行的 runner 数量上限"
            value={runner.concurrency}
            onChange={(v) => { setRunner({ concurrency: v }); markDirty(); }}
            min={1}
            max={32}
            unit="个"
            disabled={!isAdmin}
          />
          <NumberField
            label="runner_start_rate（启动速率）"
            description="每秒最多启动多少个 runner"
            value={runner.start_rate}
            onChange={(v) => { setRunner({ start_rate: v }); markDirty(); }}
            min={1}
            max={20}
            unit="个/秒"
            disabled={!isAdmin}
          />
          <StringField
            label="runner_timeout_seconds（超时开关）"
            description="off/0 表示不限制；auto 表示启用动态超时；数字表示固定秒数"
            value={runner.timeout_seconds}
            onChange={(v) => { setRunner({ timeout_seconds: v }); markDirty(); }}
            placeholder="off / auto / 240"
            disabled={!isAdmin}
          />
          <NumberField
            label="dynamic_timeout_min（动态下限）"
            description="动态算出来再短，也至少给这么多秒"
            value={runner.dynamic_timeout_min}
            onChange={(v) => { setRunner({ dynamic_timeout_min: v }); markDirty(); }}
            min={10}
            max={3600}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="dynamic_timeout_safety_margin（动态安全边际）"
            description="动态超时 = 预估耗时 × 这个倍率"
            value={runner.dynamic_timeout_safety_margin}
            onChange={(v) => { setRunner({ dynamic_timeout_safety_margin: v }); markDirty(); }}
            min={1}
            max={10}
            unit="倍"
            disabled={!isAdmin}
          />
          <NumberField
            label="dynamic_timeout_max（动态上限）"
            description="动态算出来再长，也最多给这么多秒"
            value={runner.dynamic_timeout_max}
            onChange={(v) => { setRunner({ dynamic_timeout_max: v }); markDirty(); }}
            min={60}
            max={7200}
            unit="秒"
            disabled={!isAdmin}
          />
          <ChoiceField
            label="runner_failure_policy（失败策略）"
            description="runner 失败后的处理策略"
            value={runner.failure_policy}
            choices={["retry", "fail", "ignore", "escalate"]}
            onChange={(v) => { setRunner({ failure_policy: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* Daemon */}
      <AdminSection
        icon={Server}
        title="后台任务（Daemon）"
        subtitle="Daemon 自动执行的后台任务参数"
      >
        <div className="space-y-4">
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <ToggleField
              label="daemon_planner（启用 Planner）"
              description="Daemon 是否自动执行规划任务"
              checked={daemon.planner}
              onChange={(v) => { setDaemon({ planner: v }); markDirty(); }}
              disabled={!isAdmin}
            />
            <ToggleField
              label="daemon_apply（启用 Apply）"
              description="Daemon 是否自动应用变更"
              checked={daemon.apply}
              onChange={(v) => { setDaemon({ apply: v }); markDirty(); }}
              disabled={!isAdmin}
            />
            <ToggleField
              label="daemon_execute_runners（启用 Runner）"
              description="Daemon 是否自动执行 runner"
              checked={daemon.execute_runners}
              onChange={(v) => { setDaemon({ execute_runners: v }); markDirty(); }}
              disabled={!isAdmin}
            />
            <ToggleField
              label="daemon_probe（启用探针）"
              description="Daemon 是否启用探针监控"
              checked={daemon.probe}
              onChange={(v) => { setDaemon({ probe: v }); markDirty(); }}
              disabled={!isAdmin}
            />
            <ToggleField
              label="daemon_reviewer（启用审核者）"
              description="Daemon 是否启用自动审核"
              checked={daemon.reviewer}
              onChange={(v) => { setDaemon({ reviewer: v }); markDirty(); }}
              disabled={!isAdmin}
            />
          </div>
          <Divider />
          <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
            <NumberField
              label="daemon_interval（执行间隔）"
              description="Daemon 主循环执行间隔"
              value={daemon.interval}
              onChange={(v) => { setDaemon({ interval: v }); markDirty(); }}
              min={5}
              max={3600}
              unit="秒"
              disabled={!isAdmin}
            />
            <NumberField
              label="daemon_max_runners（最大 runners）"
              description="Daemon 同时启动的最大 runner 数"
              value={daemon.max_runners}
              onChange={(v) => { setDaemon({ max_runners: v }); markDirty(); }}
              min={1}
              max={50}
              unit="个"
              disabled={!isAdmin}
            />
            <NumberField
              label="daemon_limit（处理上限）"
              description="Daemon 每轮处理的最大任务数"
              value={daemon.limit}
              onChange={(v) => { setDaemon({ limit: v }); markDirty(); }}
              min={1}
              max={200}
              unit="个"
              disabled={!isAdmin}
            />
            <NumberField
              label="daemon_max_cycles（最大周期数）"
              description="Daemon 最多运行多少周期后自动停止"
              value={daemon.max_cycles}
              onChange={(v) => { setDaemon({ max_cycles: v }); markDirty(); }}
              min={1}
              max={10000}
              unit="周期"
              disabled={!isAdmin}
            />
            <NumberField
              label="daemon_max_cards（最大卡片数）"
              description="Daemon 维护的最大任务卡片数"
              value={daemon.max_cards}
              onChange={(v) => { setDaemon({ max_cards: v }); markDirty(); }}
              min={1}
              max={500}
              unit="张"
              disabled={!isAdmin}
            />
          </div>
        </div>
      </AdminSection>
    </div>
  );
}

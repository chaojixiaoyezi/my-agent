import { useSettingsStore } from "../../stores/settingsStore";
import { useAuthStore } from "../../stores/authStore";
import {
  AdminSection,
  NumberField,
  ToggleField,
} from "../../components/settings/SettingsFieldComponents";
import { SettingsPageHeader } from "../../components/settings/SettingsPageHeader";
import { useSettingsSection } from "../../components/settings/useSettingsSection";
import { useDirtyGuard } from "../../components/settings/useDirtyGuard";
import { Server } from "lucide-react";

export default function SettingsGateway() {
  const isAdmin = useAuthStore((s) =>
    s.isAdmin);
  const gw = useSettingsStore((s) =>
    s.gatewayParams);
  const setGw = useSettingsStore((s) =>
    s.setGatewayParams);
  const wd = useSettingsStore((s) =>
    s.watchdogParams);
  const setWd = useSettingsStore((s) =>
    s.setWatchdogParams);
  const errors = useSettingsStore((s) =>
    s.errors);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("网关参数");
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="网关参数（Gateway）" subtitle="Gateway 监听端口、心跳、租约与看门狗监控" saving={saving} onSave={handleSave} dirty={dirty} />

      {/* Gateway Core */}
      <AdminSection
        icon={Server}
        title="网关核心（Gateway Core）"
        subtitle="端口、心跳和超时配置"
      >
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
          <NumberField
            label="gateway_port（监听端口）"
            description="Gateway HTTP 服务绑定的端口"
            value={gw.port}
            onChange={(v) => { setGw({ port: v }); markDirty(); }}
            min={1024}
            max={65535}
            unit=""
            disabled={!isAdmin}
            error={errors["gw_port"]}
          />
          <NumberField
            label="heartbeat_interval（心跳间隔）"
            description="Gateway 心跳写入的频率"
            value={gw.heartbeat_interval}
            onChange={(v) => { setGw({ heartbeat_interval: v }); markDirty(); }}
            min={1}
            max={60}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="gateway_stale_seconds（过期判定秒数）"
            description="多久无心跳视为节点过期"
            value={gw.stale_seconds}
            onChange={(v) => { setGw({ stale_seconds: v }); markDirty(); }}
            min={5}
            max={600}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="gateway_stop_timeout（停止超时）"
            description="Gateway 优雅停止的最大等待时间"
            value={gw.stop_timeout}
            onChange={(v) => { setGw({ stop_timeout: v }); markDirty(); }}
            min={1}
            max={300}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="gateway_processing_timeout_seconds（处理超时）"
            description="单请求处理的最大时间"
            value={gw.processing_timeout_seconds}
            onChange={(v) => { setGw({ processing_timeout_seconds: v }); markDirty(); }}
            min={10}
            max={3600}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="gateway_request_max_attempts（最大重试次数）"
            description="请求失败后的最大重试次数"
            value={gw.request_max_attempts}
            onChange={(v) => { setGw({ request_max_attempts: v }); markDirty(); }}
            min={1}
            max={10}
            unit="次"
            disabled={!isAdmin}
          />
          <NumberField
            label="lease_heartbeat_interval_seconds（租约心跳间隔）"
            description="租约续期心跳的发送间隔"
            value={gw.lease_heartbeat_interval_seconds}
            onChange={(v) => { setGw({ lease_heartbeat_interval_seconds: v }); markDirty(); }}
            min={1}
            max={120}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="lease_stale_without_heartbeat_seconds（租约过期秒数）"
            description="多久无租约心跳视为过期"
            value={gw.lease_stale_without_heartbeat_seconds}
            onChange={(v) => { setGw({ lease_stale_without_heartbeat_seconds: v }); markDirty(); }}
            min={5}
            max={300}
            unit="秒"
            disabled={!isAdmin}
          />
          <ToggleField
            label="enable_watchdog（启用看门狗）"
            description="是否启用 Gateway 进程看门狗监控"
            checked={gw.enable_watchdog}
            onChange={(v) => { setGw({ enable_watchdog: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* Watchdog */}
      <AdminSection
        icon={Server}
        title="看门狗（Watchdog）"
        subtitle="进程监控与自动重启策略"
      >
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <ToggleField
            label="watchdog_enabled（启用看门狗）"
            description="是否启用独立看门狗进程"
            checked={wd.enabled}
            onChange={(v) => { setWd({ enabled: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <NumberField
            label="watchdog_interval（检查间隔）"
            description="看门狗检查进程健康的间隔"
            value={wd.interval}
            onChange={(v) => { setWd({ interval: v }); markDirty(); }}
            min={5}
            max={300}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="watchdog_max_restarts（最大重启次数）"
            description="看门狗最多自动重启多少次"
            value={wd.max_restarts}
            onChange={(v) => { setWd({ max_restarts: v }); markDirty(); }}
            min={1}
            max={100}
            unit="次"
            disabled={!isAdmin}
          />
          <NumberField
            label="watchdog_restart_delay（重启延迟）"
            description="每次重启前的等待时间"
            value={wd.restart_delay}
            onChange={(v) => { setWd({ restart_delay: v }); markDirty(); }}
            min={1}
            max={300}
            unit="秒"
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>
    </div>
  );
}

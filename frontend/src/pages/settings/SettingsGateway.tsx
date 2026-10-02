import { useSettingsStore } from "../../stores/settingsStore";
import { useAuthStore } from "../../stores/authStore";
import {
  AdminSection,
  NumberField,
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
            label="lease_stale_without_heartbeat_seconds（租约过期秒数）"
            description="多久无租约心跳视为过期"
            value={gw.lease_stale_without_heartbeat_seconds}
            onChange={(v) => { setGw({ lease_stale_without_heartbeat_seconds: v }); markDirty(); }}
            min={5}
            max={300}
            unit="秒"
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>
    </div>
  );
}

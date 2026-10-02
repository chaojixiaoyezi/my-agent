import { useSettingsStore } from "../../stores/settingsStore";
import { useAuthStore } from "../../stores/authStore";
import {
  AdminSection,
  NumberField,
  StringField,
} from "../../components/settings/SettingsFieldComponents";
import { SettingsPageHeader } from "../../components/settings/SettingsPageHeader";
import { useSettingsSection } from "../../components/settings/useSettingsSection";
import { useDirtyGuard } from "../../components/settings/useDirtyGuard";
import { Activity } from "lucide-react";

export default function SettingsDispatch() {
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const runner = useSettingsStore((s) => s.runnerParams);
  const setRunner = useSettingsStore((s) => s.setRunnerParams);
  const errors = useSettingsStore((s) => s.errors);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("调度参数");
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="调度参数（Dispatch）" subtitle="Runner 调度策略" saving={saving} onSave={handleSave} dirty={dirty} />

      {/* Runner / Scheduler */}
      <AdminSection
        icon={Activity}
        title="Runner / Scheduler"
        subtitle="执行器并发控制与调度策略"
      >
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
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
            label="dynamic_timeout_max（动态上限）"
            description="动态算出来再长，也最多给这么多秒"
            value={runner.dynamic_timeout_max}
            onChange={(v) => { setRunner({ dynamic_timeout_max: v }); markDirty(); }}
            min={60}
            max={7200}
            unit="秒"
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>
    </div>
  );
}

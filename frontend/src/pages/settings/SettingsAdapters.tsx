import { useSettingsStore } from "../../stores/settingsStore";
import { useAuthStore } from "../../stores/authStore";
import {
  AdminSection,
  StringField,
} from "../../components/settings/SettingsFieldComponents";
import { SettingsPageHeader } from "../../components/settings/SettingsPageHeader";
import { useSettingsSection } from "../../components/settings/useSettingsSection";
import { useDirtyGuard } from "../../components/settings/useDirtyGuard";
import { Puzzle } from "lucide-react";

export default function SettingsAdapters() {
  const isAdmin = useAuthStore((s) =>
    s.isAdmin);
  const adapters = useSettingsStore((s) =>
    s.adapterParams);
  const setAdapters = useSettingsStore((s) =>
    s.setAdapterParams);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("适配器");
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="适配器（Adapters）" subtitle="第三方平台适配器配置：飞书（Feishu）、QQ 等" saving={saving} onSave={handleSave} dirty={dirty} />

      {/* General Adapter */}
      <AdminSection
        icon={Puzzle}
        title="通用适配器（General）"
        subtitle="适配器工作区与通用配置"
      >
        <div className="grid grid-cols-2 gap-4">
          <StringField
            label="adapter_workspace（适配器工作区）"
            description="适配器模块的工作区根目录"
            value={adapters.workspace}
            onChange={(v) => { setAdapters({ workspace: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* Feishu */}
      <AdminSection
        icon={Puzzle}
        title="飞书（Feishu）"
        subtitle="飞书应用凭证与 Webhook 配置"
      >
        <div className="grid grid-cols-2 gap-4">
          <StringField
            label="feishu_app_id（应用 ID）"
            description="飞书开放平台的应用 ID"
            value={adapters.feishu_app_id}
            onChange={(v) => { setAdapters({ feishu_app_id: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <StringField
            label="feishu_app_secret（应用密钥）"
            description="飞书开放平台的应用密钥"
            value={adapters.feishu_app_secret}
            onChange={(v) => { setAdapters({ feishu_app_secret: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>
    </div>
  );
}

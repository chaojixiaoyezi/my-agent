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
          <StringField
            label="feishu_webhook_url（Webhook URL）"
            description="飞书自定义机器人的 Webhook 地址"
            value={adapters.feishu_webhook_url}
            onChange={(v) => { setAdapters({ feishu_webhook_url: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* QQ */}
      <AdminSection
        icon={Puzzle}
        title="QQ"
        subtitle="QQ 机器人配置与群聊白名单"
      >
        <div className="grid grid-cols-2 gap-4">
          <StringField
            label="qq_bot_uin（机器人 QQ 号）"
            description="QQ 机器人的 QQ 号码"
            value={adapters.qq_bot_uin}
            onChange={(v) => { setAdapters({ qq_bot_uin: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <StringField
            label="qq_http_api_url（HTTP API 地址）"
            description="go-cqhttp 或 mirai 的 HTTP API 地址"
            value={adapters.qq_http_api_url}
            onChange={(v) => { setAdapters({ qq_http_api_url: v }); markDirty(); }}
            placeholder="http://localhost:5700"
            disabled={!isAdmin}
          />
          <StringField
            label="qq_group_whitelist（群聊白名单）"
            description="允许交互的 QQ 群号列表"
            value={adapters.qq_group_whitelist}
            onChange={(v) => { setAdapters({ qq_group_whitelist: v }); markDirty(); }}
            placeholder="多个群号用逗号分隔"
            disabled={!isAdmin}
          />
          <StringField
            label="qq_admin_qq（管理员 QQ）"
            description="拥有管理权限的 QQ 号"
            value={adapters.qq_admin_qq}
            onChange={(v) => { setAdapters({ qq_admin_qq: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>
    </div>
  );
}

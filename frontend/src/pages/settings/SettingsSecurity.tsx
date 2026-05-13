import { useSettingsStore } from "../../stores/settingsStore";
import { useAuthStore } from "../../stores/authStore";
import {
  AdminSection,
  NumberField,
  StringField,
  ToggleField,
} from "../../components/settings/SettingsFieldComponents";
import { SettingsPageHeader } from "../../components/settings/SettingsPageHeader";
import { useSettingsSection } from "../../components/settings/useSettingsSection";
import { useDirtyGuard } from "../../components/settings/useDirtyGuard";
import { Shield, CheckCircle, AlertTriangle } from "lucide-react";

export default function SettingsSecurity() {
  const isAdmin = useAuthStore((s) =>
    s.isAdmin);
  const sec = useSettingsStore((s) =>
    s.securityParams);
  const setSec = useSettingsStore((s) =>
    s.setSecurityParams);
  const localStore = useSettingsStore((s) =>
    s.localStoreParams);
  const setLocalStore = useSettingsStore((s) =>
    s.setLocalStoreParams);
  const notif = useSettingsStore((s) =>
    s.notificationParams);
  const setNotif = useSettingsStore((s) =>
    s.setNotificationParams);
  const audit = useSettingsStore((s) =>
    s.auditParams);
  const setAudit = useSettingsStore((s) =>
    s.setAuditParams);
  const session = useSettingsStore((s) =>
    s.sessionParams);
  const setSession = useSettingsStore((s) =>
    s.setSessionParams);
  const errors = useSettingsStore((s) =>
    s.errors);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("安全策略");
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="安全策略（Security）" subtitle="输入校验、注入防护、本地存储、通知与审计日志" saving={saving} onSave={handleSave} dirty={dirty} />

      {/* Security Policy */}
      <AdminSection
        icon={Shield}
        title="安全策略（Security Policy）"
        subtitle="输入校验、注入防护和权限规则"
      >
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <NumberField
              label="max_input_length（输入长度上限）"
              description="用户输入和 prompt 的最大字符长度"
              value={sec.max_input_length}
              onChange={(v) => { setSec({ max_input_length: v }); markDirty(); }}
              min={64}
              max={65536}
              unit="字符"
              disabled={!isAdmin}
              error={errors["sec_input"]}
            />
            <NumberField
              label="max_path_length（路径长度上限）"
              description="文件路径参数的最大字符长度"
              value={sec.max_path_length}
              onChange={(v) => { setSec({ max_path_length: v }); markDirty(); }}
              min={64}
              max={4096}
              unit="字符"
              disabled={!isAdmin}
              error={errors["sec_path"]}
            />
          </div>
          <div className="space-y-1">
            <ToggleField
              label="forbid_dangerous_chars（禁止危险字符）"
              description="输入中禁止出现 ; | & $ ` < > 等注入相关字符"
              checked={sec.forbid_dangerous_chars}
              onChange={(v) => { setSec({ forbid_dangerous_chars: v }); markDirty(); }}
              disabled={!isAdmin}
            />
            <ToggleField
              label="path_whitelist_only（路径白名单模式）"
              description="路径仅允许字母、数字、_ - / . ~ 字符"
              checked={sec.path_whitelist_only}
              onChange={(v) => { setSec({ path_whitelist_only: v }); markDirty(); }}
              disabled={!isAdmin}
            />
          </div>
          <div className="mt-2 p-2.5 rounded-lg bg-accent-green/[0.03] border border-accent-green/10 text-[11px] text-accent-green/80 flex items-start gap-1.5">
            <CheckCircle size={12} className="mt-0.5 shrink-0" />
            当前前端已启用：React JSX 自动转义（escape）、路径遍历检测（path traversal）、命令注入字符拦截（command injection filter）、DOM XSS 防护（无 dangerouslySetInnerHTML）。
          </div>
        </div>
      </AdminSection>

      {/* Session */}
      <AdminSection
        icon={Shield}
        title="会话（Session）"
        subtitle="工作区路径与会话配置"
      >
        <div className="grid grid-cols-2 gap-4">
          <StringField
            label="session_workspace（工作区路径）"
            description="默认会话工作区根目录"
            value={session.workspace}
            onChange={(v) => { setSession({ workspace: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* Local Store */}
      <AdminSection
        icon={Shield}
        title="本地存储（Local Store）"
        subtitle="本地文件存储路径与全文搜索"
      >
        <div className="grid grid-cols-2 gap-4">
          <StringField
            label="local_store_path（存储路径）"
            description="本地存储的根目录路径"
            value={localStore.store_path}
            onChange={(v) => { setLocalStore({ store_path: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <StringField
            label="local_store_files_dir（文件目录）"
            description="本地存储文件子目录"
            value={localStore.files_dir}
            onChange={(v) => { setLocalStore({ files_dir: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <StringField
            label="local_store_events_path（事件日志路径）"
            description="本地存储事件日志文件路径"
            value={localStore.events_path}
            onChange={(v) => { setLocalStore({ events_path: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <ToggleField
            label="local_store_fts_enabled（启用全文搜索）"
            description="是否启用本地存储的全文搜索（FTS）"
            checked={localStore.fts_enabled}
            onChange={(v) => { setLocalStore({ fts_enabled: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* Notifications */}
      <AdminSection
        icon={Shield}
        title="通知（Notifications）"
        subtitle="通知存储与通道超时"
      >
        <div className="grid grid-cols-2 gap-4">
          <StringField
            label="notification_store_path（通知存储路径）"
            description="通知持久化的存储路径"
            value={notif.store_path}
            onChange={(v) => { setNotif({ store_path: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <NumberField
            label="notification_channel_timeout_seconds（通道超时）"
            description="通知通道等待响应的超时时间"
            value={notif.channel_timeout_seconds}
            onChange={(v) => { setNotif({ channel_timeout_seconds: v }); markDirty(); }}
            min={1}
            max={300}
            unit="秒"
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* Audit */}
      <AdminSection
        icon={Shield}
        title="审计（Audit）"
        subtitle="审计日志路径与启用开关"
      >
        <div className="grid grid-cols-2 gap-4">
          <StringField
            label="audit_log_path（审计日志路径）"
            description="审计日志文件输出路径"
            value={audit.log_path}
            onChange={(v) => { setAudit({ log_path: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <ToggleField
            label="audit_enabled（启用审计）"
            description="是否启用操作审计日志"
            checked={audit.enabled}
            onChange={(v) => { setAudit({ enabled: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
        <div className="mt-3 p-2.5 rounded-lg bg-accent-orange/[0.03] border border-accent-orange/10 text-[11px] text-accent-orange/80 flex items-start gap-1.5">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />
          启用审计日志会记录所有敏感操作，请确保 audit_log_path 所在目录有写权限并定期清理。
        </div>
      </AdminSection>
    </div>
  );
}

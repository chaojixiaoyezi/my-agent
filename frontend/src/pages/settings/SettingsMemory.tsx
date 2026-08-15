import { useSettingsStore } from "../../stores/settingsStore";
import { useAuthStore } from "../../stores/authStore";
import {
  AdminSection,
  NumberField,
  ChoiceField,
  ToggleField,
} from "../../components/settings/SettingsFieldComponents";
import { SettingsPageHeader } from "../../components/settings/SettingsPageHeader";
import { useSettingsSection } from "../../components/settings/useSettingsSection";
import { useDirtyGuard } from "../../components/settings/useDirtyGuard";
import { Database } from "lucide-react";

export default function SettingsMemory() {
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const mem = useSettingsStore((s) => s.memoryParams);
  const setMem = useSettingsStore((s) => s.setMemoryParams);
  const adv = useSettingsStore((s) => s.memoryAdvanced);
  const setAdv = useSettingsStore((s) => s.setMemoryAdvanced);
  const errors = useSettingsStore((s) => s.errors);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("记忆参数");
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="记忆参数（Memory）" subtitle="记忆存储上限、保留策略、归档规则与高级路由" saving={saving} onSave={handleSave} dirty={dirty} />

      {/* Basic Memory */}
      <AdminSection
        icon={Database}
        title="基础记忆（Basic Memory）"
        subtitle="控制记忆存储上限、保留策略和归档"
      >
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
          <NumberField
            label="memory_limit（记忆条目上限）"
            description="单代理保留的最大记忆条目数"
            value={mem.limit}
            onChange={(v) => { setMem({ limit: v }); markDirty(); }}
            min={100}
            max={50000}
            unit="条"
            disabled={!isAdmin}
            error={errors["mem_limit"]}
          />
          <NumberField
            label="retention_days（保留天数）"
            description="记忆自动清理前的保留天数"
            value={mem.retention_days}
            onChange={(v) => { setMem({ retention_days: v }); markDirty(); }}
            min={1}
            max={365}
            unit="天"
            disabled={!isAdmin}
          />
          <ToggleField
            label="enable_archive（启用归档）"
            description="超出上限的记忆是否自动归档到磁盘"
            checked={mem.enable_archive}
            onChange={(v) => { setMem({ enable_archive: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* Advanced Memory */}
      <AdminSection
        icon={Database}
        title="高级记忆（Advanced Memory）"
        subtitle="路由规则、Hook、上下文恢复与压缩策略"
      >
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
          <NumberField
            label="memory_top_k（Top-K 检索）"
            description="记忆检索时返回的最相关条目数"
            value={adv.top_k}
            onChange={(v) => { setAdv({ top_k: v }); markDirty(); }}
            min={1}
            max={50}
            unit="条"
            disabled={!isAdmin}
          />
          <ChoiceField
            label="memory_archive_level（归档级别）"
            description="记忆归档的详细程度"
            value={adv.archive_level}
            choices={["auto", "full", "summary", "none"]}
            onChange={(v) => { setAdv({ archive_level: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <ToggleField
            label="memory_hook_enabled（启用 Hook）"
            description="是否启用记忆 Hook 机制"
            checked={adv.hook_enabled}
            onChange={(v) => { setAdv({ hook_enabled: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <ChoiceField
            label="memory_hook_archive_level（Hook 归档级别）"
            description="Hook 触发时归档的详细程度"
            value={adv.hook_archive_level}
            choices={["auto", "full", "summary", "none"]}
            onChange={(v) => { setAdv({ hook_archive_level: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <NumberField
            label="memory_hook_retention_days（Hook 保留天数）"
            description="Hook 记忆的保留天数"
            value={adv.hook_retention_days}
            onChange={(v) => { setAdv({ hook_retention_days: v }); markDirty(); }}
            min={1}
            max={365}
            unit="天"
            disabled={!isAdmin}
          />
          <ToggleField
            label="memory_rule_routing_enabled（启用规则路由）"
            description="是否启用基于规则的记忆路由"
            checked={adv.rule_routing_enabled}
            onChange={(v) => { setAdv({ rule_routing_enabled: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <ChoiceField
            label="memory_rule_routing_mode（路由模式）"
            description="规则路由的匹配模式"
            value={adv.rule_routing_mode}
            choices={["default", "exact", "prefix", "regex"]}
            onChange={(v) => { setAdv({ rule_routing_mode: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <NumberField
            label="memory_rule_auto_read_limit（自动读取限制）"
            description="规则匹配时自动读取的最大条数"
            value={adv.rule_auto_read_limit}
            onChange={(v) => { setAdv({ rule_auto_read_limit: v }); markDirty(); }}
            min={1}
            max={100}
            unit="条"
            disabled={!isAdmin}
          />
          <ToggleField
            label="memory_rule_receipt_enabled（启用回执）"
            description="记忆操作是否发送回执确认"
            checked={adv.rule_receipt_enabled}
            onChange={(v) => { setAdv({ rule_receipt_enabled: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <ToggleField
            label="memory_resume_auto_context_enabled（自动上下文恢复）"
            description="会话恢复时是否自动加载上下文"
            checked={adv.resume_auto_context_enabled}
            onChange={(v) => { setAdv({ resume_auto_context_enabled: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <ChoiceField
            label="memory_resume_auto_context_mode（恢复模式）"
            description="上下文恢复的选择策略"
            value={adv.resume_auto_context_mode}
            choices={["recent", "relevant", "full", "none"]}
            onChange={(v) => { setAdv({ resume_auto_context_mode: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <NumberField
            label="memory_resume_auto_context_limit（恢复条数限制）"
            description="自动恢复时加载的最大上下文条数"
            value={adv.resume_auto_context_limit}
            onChange={(v) => { setAdv({ resume_auto_context_limit: v }); markDirty(); }}
            min={1}
            max={200}
            unit="条"
            disabled={!isAdmin}
          />
          <NumberField
            label="memory_compact_auto_trigger_percent（自动压缩阈值）"
            description="上下文使用到多少百分比时自动压缩，0 表示 100%"
            value={adv.compact_auto_trigger_percent}
            onChange={(v) => { setAdv({ compact_auto_trigger_percent: v }); markDirty(); }}
            min={0}
            max={100}
            unit="%"
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>
    </div>
  );
}

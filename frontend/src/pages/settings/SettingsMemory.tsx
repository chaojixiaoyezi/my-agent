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
  const adv = useSettingsStore((s) => s.memoryAdvanced);
  const setAdv = useSettingsStore((s) => s.setMemoryAdvanced);
  const errors = useSettingsStore((s) => s.errors);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("记忆参数");
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="记忆参数（Memory）" subtitle="路由规则、Hook、上下文恢复与压缩策略" saving={saving} onSave={handleSave} dirty={dirty} />

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
          <ChoiceField
            label="memory_resume_auto_context_mode（恢复模式）"
            description="上下文恢复的选择策略"
            value={adv.resume_auto_context_mode}
            choices={["recent", "relevant", "full", "none"]}
            onChange={(v) => { setAdv({ resume_auto_context_mode: v }); markDirty(); }}
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

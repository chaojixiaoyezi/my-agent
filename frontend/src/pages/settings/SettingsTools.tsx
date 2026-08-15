import { useSettingsStore } from "../../stores/settingsStore";
import { useAuthStore } from "../../stores/authStore";
import {
  AdminSection,
  ChoiceField,
  NumberField,
  StringField,
  ToggleField,
} from "../../components/settings/SettingsFieldComponents";
import { SettingsPageHeader } from "../../components/settings/SettingsPageHeader";
import { useSettingsSection } from "../../components/settings/useSettingsSection";
import { useDirtyGuard } from "../../components/settings/useDirtyGuard";
import { Wrench, AlertTriangle } from "lucide-react";

export default function SettingsTools() {
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const budget = useSettingsStore((s) => s.toolBudget);
  const setBudget = useSettingsStore((s) => s.setToolBudget);
  const limits = useSettingsStore((s) => s.toolLimits);
  const setLimits = useSettingsStore((s) => s.setToolLimits);
  const advanced = useSettingsStore((s) => s.toolAdvanced);
  const setAdvanced = useSettingsStore((s) => s.setToolAdvanced);
  const errors = useSettingsStore((s) => s.errors);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("工具参数");
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="工具参数（Tools）" subtitle="工具预算、调用限制与高级工具配置" saving={saving} onSave={handleSave} dirty={dirty} />

      {/* Tool Budget */}
      <AdminSection
        icon={Wrench}
        title="工具预算（Tool Budget）"
        subtitle="控制代理在滚动窗口内的工具调用频率"
      >
        <div className="grid grid-cols-2 gap-4">
          <NumberField
            label="tool_agent_budget_window_seconds（预算窗口）"
            description="单个代理的滚动工具预算时间窗口"
            value={budget.window_seconds}
            onChange={(v) => { setBudget({ window_seconds: v }); markDirty(); }}
            min={10}
            max={3600}
            unit="秒"
            disabled={!isAdmin}
            error={errors["tool_window"]}
          />
          <NumberField
            label="tool_agent_budget_max_calls（最大调用次数）"
            description="预算窗口内最多调用的工具次数"
            value={budget.max_calls}
            onChange={(v) => { setBudget({ max_calls: v }); markDirty(); }}
            min={1}
            max={1000}
            unit="次"
            disabled={!isAdmin}
            error={errors["tool_calls"]}
          />
          <NumberField
            label="tool_artifact_read_budget_window_seconds（产物读取窗口）"
            description="外置产物读取预算的滚动时间窗口"
            value={budget.artifact_read_window_seconds}
            onChange={(v) => { setBudget({ artifact_read_window_seconds: v }); markDirty(); }}
            min={10}
            max={3600}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_artifact_read_budget_max_chars（产物读取字符预算）"
            description="预算窗口内最多读取多少外置产物字符"
            value={budget.artifact_read_max_chars}
            onChange={(v) => { setBudget({ artifact_read_max_chars: v }); markDirty(); }}
            min={1000}
            max={2000000}
            unit="字符"
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      {/* Tool Limits */}
      <AdminSection
        icon={Wrench}
        title="工具限制（Tool Limits）"
        subtitle="各类工具的单次调用限制"
      >
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
          <NumberField
            label="tool_write_inline_max_chars（写入最大字符数）"
            description="write_file 内联写入的最大字符数"
            value={limits.write_inline_max_chars}
            onChange={(v) => { setLimits({ write_inline_max_chars: v }); markDirty(); }}
            min={100}
            max={100000}
            unit="字符"
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_read_max_chars（读取最大字符数）"
            description="read_file 单次读取的最大字符数"
            value={limits.read_max_chars}
            onChange={(v) => { setLimits({ read_max_chars: v }); markDirty(); }}
            min={1000}
            max={500000}
            unit="字符"
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_list_max_entries（列表最大条目数）"
            description="list_files 返回的最大条目数"
            value={limits.list_max_entries}
            onChange={(v) => { setLimits({ list_max_entries: v }); markDirty(); }}
            min={10}
            max={1000}
            unit="条"
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_http_timeout（HTTP 超时）"
            description="http_request 工具的超时时间"
            value={limits.http_timeout}
            onChange={(v) => { setLimits({ http_timeout: v }); markDirty(); }}
            min={1}
            max={300}
            unit="秒"
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_shell_timeout（Shell 超时）"
            description="controlled_exec 工具的默认超时时间"
            value={limits.shell_timeout}
            onChange={(v) => { setLimits({ shell_timeout: v }); markDirty(); }}
            min={1}
            max={600}
            unit="秒"
            disabled={!isAdmin}
          />
        </div>
        <div className="mt-3 p-2.5 rounded-lg bg-accent-orange/[0.03] border border-accent-orange/10 text-[11px] text-accent-orange/80 flex items-start gap-1.5">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />
          提高 tool_agent_budget_max_calls 或放宽字符限制会增加模型滥用风险，请谨慎调整。
        </div>
      </AdminSection>

      {/* Advanced Tool Params */}
      <AdminSection
        icon={Wrench}
        title="高级工具配置（Advanced Tools）"
        subtitle="搜索、检索和向量搜索等高级功能"
      >
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
          <NumberField
            label="tool_search_max_matches（搜索最大匹配数）"
            description="工具搜索返回的最大匹配结果数"
            value={advanced.search_max_matches}
            onChange={(v) => { setAdvanced({ search_max_matches: v }); markDirty(); }}
            min={1}
            max={100}
            unit="条"
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_web_max_chars（Web 最大字符数）"
            description="Web 抓取内容的最大字符数"
            value={advanced.web_max_chars}
            onChange={(v) => { setAdvanced({ web_max_chars: v }); markDirty(); }}
            min={1000}
            max={500000}
            unit="字符"
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_catalog_limit（目录限制）"
            description="工具目录返回的最大条目数"
            value={advanced.catalog_limit}
            onChange={(v) => { setAdvanced({ catalog_limit: v }); markDirty(); }}
            min={10}
            max={500}
            unit="条"
            disabled={!isAdmin}
          />
          <ChoiceField
            label="tool_catalog_mode（目录模式）"
            description="compact 精简、full 完整、retrieval_only 只靠召回、off 关闭目录"
            value={advanced.catalog_mode}
            choices={["compact", "full", "retrieval_only", "off"]}
            onChange={(v) => { setAdvanced({ catalog_mode: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_catalog_offset（目录分页偏移）"
            description="工具目录从第几个条目开始展示"
            value={advanced.catalog_offset}
            onChange={(v) => { setAdvanced({ catalog_offset: v }); markDirty(); }}
            min={0}
            max={500}
            unit="条"
            disabled={!isAdmin}
          />
          <StringField
            label="tool_catalog_categories（目录类别过滤）"
            description="逗号分隔；空值表示不过滤，例如 filesystem, network"
            value={advanced.catalog_categories.join(", ")}
            onChange={(v) => {
              setAdvanced({ catalog_categories: v.split(",").map((item) => item.trim()).filter(Boolean) });
              markDirty();
            }}
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_catalog_entry_max_chars（单条目录上限）"
            description="单个工具目录条目的最大字符数，0 表示不截断"
            value={advanced.catalog_entry_max_chars}
            onChange={(v) => { setAdvanced({ catalog_entry_max_chars: v }); markDirty(); }}
            min={0}
            max={10000}
            unit="字符"
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_detail_max_chars（工具详情上限）"
            description="Recommended Tools 单个详细工具说明的最大字符数，0 表示不截断"
            value={advanced.tool_detail_max_chars}
            onChange={(v) => { setAdvanced({ tool_detail_max_chars: v }); markDirty(); }}
            min={0}
            max={20000}
            unit="字符"
            disabled={!isAdmin}
          />
          <NumberField
            label="tool_retrieval_limit（检索限制）"
            description="知识库检索返回的最大条数"
            value={advanced.retrieval_limit}
            onChange={(v) => { setAdvanced({ retrieval_limit: v }); markDirty(); }}
            min={1}
            max={100}
            unit="条"
            disabled={!isAdmin}
          />
          <ToggleField
            label="tool_vector_search_enabled（启用向量搜索）"
            description="是否启用基于向量的语义搜索"
            checked={advanced.vector_search_enabled}
            onChange={(v) => { setAdvanced({ vector_search_enabled: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <ToggleField
            label="tool_catalog_include_examples（目录包含示例）"
            description="是否在工具目录中展示调用示例"
            checked={advanced.catalog_include_examples}
            onChange={(v) => { setAdvanced({ catalog_include_examples: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <ToggleField
            label="tool_catalog_show_truncated_notice（目录分页提示）"
            description="目录分页或截断时是否提示 next_offset"
            checked={advanced.catalog_show_truncated_notice}
            onChange={(v) => { setAdvanced({ catalog_show_truncated_notice: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>
    </div>
  );
}

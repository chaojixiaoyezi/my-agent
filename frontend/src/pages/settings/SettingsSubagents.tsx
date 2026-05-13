import { useState } from "react";
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
import { Users, GitBranch } from "lucide-react";

export default function SettingsSubagents() {
  const isAdmin = useAuthStore((s) =>
    s.isAdmin);
  const roleTemplates = useSettingsStore((s) =>
    s.roleTemplates);
  const setRoleRounds = useSettingsStore((s) =>
    s.setRoleRounds);
  const sub = useSettingsStore((s) =>
    s.subagentParams);
  const setSub = useSettingsStore((s) =>
    s.setSubagentParams);
  const acceptance = useSettingsStore((s) =>
    s.acceptanceParams);
  const setAcceptance = useSettingsStore((s) =>
    s.setAcceptanceParams);
  const workflow = useSettingsStore((s) =>
    s.workflowParams);
  const setWorkflow = useSettingsStore((s) =>
    s.setWorkflowParams);
  const errors = useSettingsStore((s) =>
    s.errors);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("子代理参数");
  const [subagentEditingEnabled, setSubagentEditingEnabled] = useState(false);
  const protectedDisabled = !isAdmin || !subagentEditingEnabled;
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="子代理参数（Subagents）" subtitle="角色模板、子代理限制、验收策略与工作流模式" saving={saving} onSave={handleSave} dirty={dirty} />
      <div className="rounded-xl border border-border bg-black/[0.02] p-3 text-xs text-ink-tertiary">
        <div className="flex items-center justify-between gap-3">
          <span>
            子代理高级参数默认保护，避免误改数量、模板和验收策略。确认要调参时可临时启用修改。
          </span>
          <button
            type="button"
            onClick={() => setSubagentEditingEnabled((value) => !value)}
            disabled={!isAdmin}
            className="shrink-0 rounded-lg border border-border bg-card px-3 py-1.5 font-medium text-ink-secondary hover:border-border-hover disabled:opacity-40"
          >
            {subagentEditingEnabled ? "关闭保护修改" : "启用修改"}
          </button>
        </div>
      </div>

      {/* Role Templates */}
      <AdminSection
        icon={Users}
        title="角色模板（Role Templates）"
        subtitle="每个角色在单次调度循环中允许的最大推理轮数（max_rounds）"
      >
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {roleTemplates.map((r) => (
            <NumberField
              key={r.name}
              label={r.label}
              description={`max_rounds（${r.name}）`}
              value={r.max_rounds}
              onChange={(v) => { setRoleRounds(r.name, v); markDirty(); }}
              min={1}
              max={100}
              unit="轮"
              disabled={protectedDisabled}
              error={errors[`role_${r.name}`]}
            />
          ))}
        </div>
      </AdminSection>

      {/* Subagent Limits */}
      <AdminSection
        icon={Users}
        title="子代理限制（Subagent Limits）"
        subtitle="任务拆分深度、并发限制与工作流目录"
      >
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
          <NumberField
            label="subagent_board_limit（看板限制）"
            description="子代理看板最大任务卡片数"
            value={sub.board_limit}
            onChange={(v) => { setSub({ board_limit: v }); markDirty(); }}
            min={10}
            max={500}
            unit="张"
            disabled={!isAdmin}
          />
          <ToggleField
            label="subagent_builtin_workflows（内置工作流）"
            description="是否启用内置工作流模板"
            checked={sub.builtin_workflows}
            onChange={(v) => { setSub({ builtin_workflows: v }); markDirty(); }}
            disabled={protectedDisabled}
          />
          <StringField
            label="subagent_user_workflow_dirs（用户工作流目录）"
            description="用户自定义工作流的目录路径"
            value={sub.user_workflow_dirs.join(", ")}
            onChange={(v) => {
              setSub({ user_workflow_dirs: v.split(",").map((item) => item.trim()).filter(Boolean) });
              markDirty();
            }}
            placeholder="多个路径用逗号分隔"
            disabled={protectedDisabled}
          />
          <NumberField
            label="subagent_workflow_review_rounds（审核轮数）"
            description="工作流产出审核的最大轮数"
            value={sub.workflow_review_rounds}
            onChange={(v) => { setSub({ workflow_review_rounds: v }); markDirty(); }}
            min={0}
            max={10}
            unit="轮"
            disabled={protectedDisabled}
          />
          <NumberField
            label="task_max_subagents（最大子代理数）"
            description="单个任务最多派生的直接子代理数"
            value={sub.task_max_subagents}
            onChange={(v) => { setSub({ task_max_subagents: v }); markDirty(); }}
            min={0}
            max={1000}
            unit="个"
            disabled={protectedDisabled}
          />
          <NumberField
            label="task_max_grandchildren（最大孙代理数）"
            description="单个任务最多派生的间接子代理总数"
            value={sub.task_max_grandchildren}
            onChange={(v) => { setSub({ task_max_grandchildren: v }); markDirty(); }}
            min={0}
            max={1000}
            unit="个"
            disabled={protectedDisabled}
          />
          <NumberField
            label="max_auto_split_depth（自动拆分深度）"
            description="任务自动拆分的最大递归深度"
            value={sub.max_auto_split_depth}
            onChange={(v) => { setSub({ max_auto_split_depth: v }); markDirty(); }}
            min={0}
            max={10}
            unit="层"
            disabled={protectedDisabled}
          />
          <NumberField
            label="max_auto_retry_attempts（自动重试次数）"
            description="子代理失败后的自动重试次数"
            value={sub.max_auto_retry_attempts}
            onChange={(v) => { setSub({ max_auto_retry_attempts: v }); markDirty(); }}
            min={0}
            max={10}
            unit="次"
            disabled={protectedDisabled}
          />
        </div>
      </AdminSection>

      {/* Acceptance & Workflow */}
      <AdminSection
        icon={GitBranch}
        title="验收与工作流（Acceptance & Workflow）"
        subtitle="验收策略、工作流模式和自学习开关"
      >
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <ChoiceField
              label="reviewer_mode（审核者模式）"
              description="验收流程由谁负责审核"
              value={acceptance.reviewer_mode}
              choices={["parent-dispatch", "auto", "human-in-loop"]}
              onChange={(v) => { setAcceptance({ reviewer_mode: v }); markDirty(); }}
              disabled={protectedDisabled}
            />
            <ToggleField
              label="auto_accept（自动验收）"
              description="低风险产出是否自动通过验收"
              checked={acceptance.auto_accept}
              onChange={(v) => { setAcceptance({ auto_accept: v }); markDirty(); }}
              disabled={protectedDisabled}
            />
          </div>
          <Divider />
          <div className="grid grid-cols-2 gap-4">
            <ChoiceField
              label="workflow_mode（工作流模式）"
              description="子代理调度的工作流策略"
              value={workflow.mode}
              choices={["off", "sequential", "parallel", "adaptive"]}
              onChange={(v) => { setWorkflow({ mode: v }); markDirty(); }}
              disabled={protectedDisabled}
            />
            <ToggleField
              label="enable_self_learning（启用自学习）"
              description="成功 runner 的 lessons 是否生成 learning draft"
              checked={workflow.enable_self_learning}
              onChange={(v) => { setWorkflow({ enable_self_learning: v }); markDirty(); }}
              disabled={protectedDisabled}
            />
          </div>
        </div>
      </AdminSection>
    </div>
  );
}

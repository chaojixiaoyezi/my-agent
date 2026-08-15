import { BookOpen, FileJson, CheckCircle } from "lucide-react";
import { runtimeRoleTemplates } from "../data/runtimeConfig";

export default function Templates() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-xl bg-accent-purple flex items-center justify-center">
          <BookOpen size={16} className="text-white" />
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink">角色模板（Role Templates）</h2>
          <p className="text-[11px] text-ink-tertiary">
            共 {runtimeRoleTemplates.length} 个内置角色
          </p>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3">
        {runtimeRoleTemplates.map((t) => (
          <div
            key={t.name}
            className="bg-card rounded-2xl border border-border p-4 shadow-card hover:shadow-card-hover hover:-translate-y-0.5 transition-all duration-200"
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium text-ink">{t.label}</span>
              {t.builtin && (
                <span className="px-1.5 py-0.5 rounded-md bg-accent-green/10 text-accent-green text-[10px] font-medium">
                  内置
                </span>
              )}
            </div>
            <div className="text-xs text-ink-secondary mb-3">{t.description}</div>
            <div className="mb-2">
              <span className="text-[10px] text-ink-tertiary">允许工具:</span>
              <div className="flex flex-wrap gap-1 mt-1">
                {t.allowed_tools.map((tool) => (
                  <span
                    key={tool}
                    className="px-1.5 py-0.5 rounded-md bg-black/[0.03] text-[10px] text-ink-secondary"
                  >
                    {tool}
                  </span>
                ))}
              </div>
            </div>
            <div className="text-[11px] text-ink-tertiary">
              max_rounds: {t.max_rounds}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-xl bg-accent-cyan flex items-center justify-center">
          <FileJson size={16} className="text-white" />
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink">工作流模板（Workflow Templates）</h2>
          <p className="text-[11px] text-ink-tertiary">预留，后续扩展</p>
        </div>
      </div>

      <div className="bg-card rounded-2xl border border-border p-8 shadow-card text-center">
        <div className="text-sm text-ink-secondary mb-1">工作流模板</div>
        <div className="text-[11px] text-ink-tertiary">当前工作流模式: auto</div>
        <div className="mt-3 flex justify-center gap-2">
          {["需求分析", "开发实现", "测试验证", "验收交付"].map((s) => (
            <div
              key={s}
              className="px-3 py-1.5 rounded-xl bg-black/[0.02] border border-border text-xs text-ink-secondary"
            >
              {s}
            </div>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-xl bg-accent-orange flex items-center justify-center">
          <CheckCircle size={16} className="text-white" />
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink">Skill 模板（预留）</h2>
          <p className="text-[11px] text-ink-tertiary">后续自学习模块生成</p>
        </div>
      </div>

      <div className="bg-card rounded-2xl border border-border p-8 shadow-card text-center">
        <div className="text-sm text-ink-secondary mb-1">Skill Templates</div>
        <div className="text-[11px] text-ink-tertiary">
          打开 enable_self_learning=true 后，成功 runner 的 lessons 会生成 learning draft 候选
        </div>
      </div>
    </div>
  );
}

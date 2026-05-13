import { useConfigStore } from "../../stores/configStore";
import { X } from "lucide-react";
import { useEscapeKey } from "../../hooks/useEscapeKey";
import { useMemo } from "react";

export function ConfigDiff() {
  const { schema, draftValues, toggleDiff } = useConfigStore();

  const diffs = useMemo(() => {
    const list: { key: string; label: string; oldVal: string; newVal: string }[] = [];
    for (const cat of schema.categories) {
      for (const f of cat.fields) {
        const oldStr = JSON.stringify(f.defaultValue);
        const newStr = JSON.stringify(draftValues[f.key]);
        if (oldStr !== newStr) {
          list.push({
            key: f.key,
            label: f.label,
            oldVal: String(f.defaultValue),
            newVal: String(draftValues[f.key]),
          });
        }
      }
    }
    return list;
  }, [schema, draftValues]);

  useEscapeKey(toggleDiff);

  if (diffs.length === 0) {
    return (
      <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/20" role="dialog" aria-modal="true" aria-label="变更对比">
        <div className="bg-card rounded-3xl shadow-card-hover p-6 w-[480px] max-h-[70vh] flex flex-col">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-semibold text-ink">变更对比</h3>
            <button onClick={toggleDiff} className="text-ink-tertiary hover:text-ink">
              <X size={16} />
            </button>
          </div>
          <p className="text-sm text-ink-secondary">暂无修改</p>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/20" role="dialog" aria-modal="true" aria-label="变更对比">
      <div className="bg-card rounded-3xl shadow-card-hover p-6 w-[560px] max-h-[70vh] flex flex-col">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-ink">变更对比（{diffs.length} 项）</h3>
          <button onClick={toggleDiff} className="text-ink-tertiary hover:text-ink" aria-label="关闭">
            <X size={16} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto space-y-3 pr-1">
          {diffs.map((d) => (
            <div key={d.key} className="rounded-xl border border-border p-3 bg-surface">
              <div className="text-xs font-medium text-ink mb-1.5">{d.label}</div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded-lg bg-black/[0.02] px-2.5 py-1.5">
                  <span className="text-ink-tertiary">默认值：</span>
                  <span className="text-ink-secondary ml-1">{d.oldVal}</span>
                </div>
                <div className="rounded-lg bg-accent-blue/[0.04] px-2.5 py-1.5 border border-accent-blue/10">
                  <span className="text-accent-blue">新值：</span>
                  <span className="text-ink ml-1 font-medium">{d.newVal}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

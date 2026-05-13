import { useConfigStore } from "../../stores/configStore";
import { cn } from "../../lib/utils";
import { X, Copy, Check } from "lucide-react";
import { useState } from "react";
import { useEscapeKey } from "../../hooks/useEscapeKey";

export function ConfigYamlModal() {
  const toggleYaml = useConfigStore((s) => s.toggleYaml);
  const getYamlExport = useConfigStore((s) => s.getYamlExport);
  const yaml = getYamlExport();
  const [copied, setCopied] = useState(false);

  useEscapeKey(toggleYaml);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(yaml);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/20" role="dialog" aria-modal="true" aria-label="YAML 导出预览">
      <div className="bg-card rounded-3xl shadow-card-hover p-6 w-[640px] max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-ink">YAML 导出预览</h3>
          <div className="flex items-center gap-2">
            <button
              onClick={handleCopy}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all duration-150",
                copied
                  ? "bg-accent-green/10 text-accent-green"
                  : "bg-surface border border-border text-ink-secondary hover:border-border-hover"
              )}
            >
              {copied ? <Check size={13} /> : <Copy size={13} />}
              {copied ? "已复制" : "复制"}
            </button>
            <button onClick={toggleYaml} className="text-ink-tertiary hover:text-ink p-1" aria-label="关闭">
              <X size={16} />
            </button>
          </div>
        </div>
        <pre className="flex-1 overflow-auto rounded-2xl bg-surface border border-border p-4 text-xs text-ink-secondary leading-relaxed font-mono">
          {yaml}
        </pre>
      </div>
    </div>
  );
}

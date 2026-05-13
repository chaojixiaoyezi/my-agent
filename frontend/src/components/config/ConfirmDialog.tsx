import { useConfigStore } from "../../stores/configStore";
import { AlertTriangle } from "lucide-react";
import { useState } from "react";
import { useEscapeKey } from "../../hooks/useEscapeKey";

export function ConfirmDialog() {
  const confirmDialog = useConfigStore((s) => s.confirmDialog);
  const closeConfirm = useConfigStore((s) => s.closeConfirm);
  const confirmSave = useConfigStore((s) => s.confirmSave);
  const [checked, setChecked] = useState(false);

  useEscapeKey(closeConfirm, confirmDialog.open);

  if (!confirmDialog.open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/20" role="dialog" aria-modal="true" aria-label="确认高风险修改">
      <div className="bg-card rounded-3xl shadow-card-hover p-6 w-[420px]">
        <div className="flex items-center gap-3 mb-3">
          <div className="w-9 h-9 rounded-xl bg-accent-red/10 flex items-center justify-center">
            <AlertTriangle size={18} className="text-accent-red" />
          </div>
          <h3 className="text-sm font-semibold text-ink">确认高风险修改</h3>
        </div>

        <p className="text-sm text-ink-secondary mb-2">
          您正在修改 <strong className="text-ink">{confirmDialog.fieldLabel}</strong>，这是一项高风险配置。
        </p>

        <div className="rounded-xl bg-accent-red/[0.04] border border-accent-red/10 p-3 mb-4">
          <p className="text-xs text-accent-red/80 leading-relaxed">{confirmDialog.riskWarning}</p>
        </div>

        <label className="flex items-start gap-2.5 mb-5 cursor-pointer">
          <input
            type="checkbox"
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
            className="mt-0.5 w-4 h-4 rounded border-border text-accent-blue focus:ring-accent-blue/20"
          />
          <span className="text-xs text-ink-secondary leading-relaxed">
            我已了解风险，确认修改
          </span>
        </label>

        <div className="flex justify-end gap-2">
          <button
            onClick={closeConfirm}
            className="px-4 py-2 rounded-xl text-sm font-medium text-ink-secondary hover:bg-black/[0.03] transition-colors"
          >
            取消
          </button>
          <button
            disabled={!checked}
            onClick={confirmSave}
            className="px-4 py-2 rounded-xl text-sm font-medium text-white bg-accent-red hover:bg-accent-red-dark disabled:opacity-40 disabled:cursor-not-allowed transition-all duration-150"
          >
            确认保存
          </button>
        </div>
      </div>
    </div>
  );
}

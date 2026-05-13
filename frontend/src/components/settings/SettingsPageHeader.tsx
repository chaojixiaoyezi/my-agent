import { Save, Loader2 } from "lucide-react";
import { cn } from "../../lib/utils";
import { PermissionGate } from "./PermissionGate";

export function SettingsPageHeader({
  title,
  subtitle,
  saving,
  onSave,
  dirty = false,
}: {
  title: string;
  subtitle: string;
  saving?: boolean;
  onSave?: () => void;
  dirty?: boolean;
}) {
  return (
    <div className="flex items-center justify-between mb-6">
      <div>
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        <p className="text-[11px] text-ink-tertiary mt-0.5">{subtitle}</p>
      </div>
      {onSave && (
        <PermissionGate requireAdmin>
          <button
            onClick={onSave}
            disabled={saving || !dirty}
            className={cn(
              "flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-medium text-white transition-all duration-150",
              saving || !dirty
                ? "bg-accent-blue/40 cursor-not-allowed"
                : "bg-accent-blue hover:bg-accent-blue-dark shadow-card hover:shadow-card-hover hover:-translate-y-px"
            )}
          >
            {saving ? (
              <>
                <Loader2 size={13} className="animate-spin" />
                保存中...
              </>
            ) : (
              <>
                <Save size={13} />
                保存
              </>
            )}
          </button>
        </PermissionGate>
      )}
    </div>
  );
}

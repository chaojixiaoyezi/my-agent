import { useState, useCallback } from "react";
import type { ConfigField } from "../../types/config";
import { useConfigStore } from "../../stores/configStore";
import { useAuthStore } from "../../stores/authStore";
import { validatePath, validateString, validateNumber } from "../../lib/validation";
import { cn } from "../../lib/utils";
import {
  AlertTriangle,
  RotateCcw,
  Eye,
  EyeOff,
  HelpCircle,
  Lock,
} from "lucide-react";

function isModified(field: ConfigField, value: unknown): boolean {
  if (value === field.defaultValue) return false;
  if (typeof value !== typeof field.defaultValue) return true;
  if (typeof value === "object" && value !== null) {
    return JSON.stringify(value) !== JSON.stringify(field.defaultValue);
  }
  return true;
}

function RiskBadge({ level, warning }: { level?: string; warning?: string }) {
  if (!level || level === "low") return null;
  const colors: Record<string, string> = {
    medium: "bg-accent-orange/10 text-accent-orange border-accent-orange/20",
    high: "bg-accent-red/10 text-accent-red border-accent-red/20",
    critical: "bg-accent-red/10 text-accent-red border-accent-red/20",
  };
  return (
    <span
      title={warning}
      className={cn(
        "inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] font-medium border ml-2",
        colors[level] || colors.medium
      )}
    >
      <AlertTriangle size={10} />
      {level === "critical" ? "严重" : level === "high" ? "高风险" : "中风险"}
    </span>
  );
}

export function ConfigFieldRenderer({ field }: { field: ConfigField }) {
  const value = useConfigStore(
    (s) => s.draftValues[field.key] ?? field.defaultValue
  );
  const setDraftValue = useConfigStore((s) => s.setDraftValue);
  const resetField = useConfigStore((s) => s.resetField);
  const showAdvanced = useConfigStore((s) => s.showAdvanced);
  const modified = isModified(field, value);
  const [showSecret, setShowSecret] = useState(false);
  const [tooltipOpen, setTooltipOpen] = useState(false);
  const [fieldUnlocked, setFieldUnlocked] = useState(!field.requiresUnlock);

  const canEditField = useAuthStore((s) => s.canEditField);
  const baseEditable = canEditField(field.key);
  const needsUnlock = Boolean(field.requiresUnlock);
  const editable = baseEditable && (!needsUnlock || fieldUnlocked);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const runValidation = useCallback(
    (v: unknown) => {
      if (field.type === "path" && typeof v === "string") {
        const res = validatePath(v);
        setFieldError(res.valid ? null : res.error || null);
        return res.valid;
      }
      if ((field.type === "string" || field.type === "secret" || field.type === "text") && typeof v === "string") {
        const res = validateString(v, {
          maxLength: field.maxLength || 2048,
          minLength: field.minLength || 0,
          allowEmpty: true,
          label: field.label,
        });
        setFieldError(res.valid ? null : res.error || null);
        return res.valid;
      }
      if (field.type === "number" && typeof v === "number") {
        const res = validateNumber(v, { min: field.min, max: field.max, label: field.label });
        setFieldError(res.valid ? null : res.error || null);
        return res.valid;
      }
      setFieldError(null);
      return true;
    },
    [field]
  );

  const handleChange = useCallback(
    (v: unknown) => {
      if (!editable) return;
      runValidation(v);
      setDraftValue(field.key, v);
    },
    [field.key, setDraftValue, editable, runValidation]
  );

  const inputBase =
    "w-full px-3 py-2 text-sm bg-surface border border-border rounded-lg text-ink placeholder:text-ink-tertiary focus:outline-none focus:border-accent-blue/40 focus:shadow-focus transition-all duration-150 disabled:bg-black/[0.02] disabled:text-ink-tertiary disabled:cursor-not-allowed";

  const renderControl = () => {
    const disabled = !editable;
    switch (field.type) {
      case "string":
      case "path":
        return (
          <input
            type="text"
            value={(value as string) ?? ""}
            onChange={(e) => handleChange(e.target.value)}
            placeholder={field.placeholder}
            disabled={disabled}
            className={cn(inputBase, fieldError && "border-accent-red focus:border-accent-red")}
          />
        );

      case "secret":
        return (
          <div className="relative">
            <input
              type={showSecret ? "text" : "password"}
              value={(value as string) ?? ""}
              onChange={(e) => handleChange(e.target.value)}
              placeholder={field.placeholder || "***"}
              disabled={disabled}
              className={cn(inputBase, "pr-9", fieldError && "border-accent-red focus:border-accent-red")}
            />
            <button
              type="button"
              onClick={() => setShowSecret(!showSecret)}
              aria-label={showSecret ? "隐藏密码" : "显示密码"}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-ink-tertiary hover:text-ink-secondary transition-colors"
            >
              {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        );

      case "text":
        return (
          <textarea
            value={(value as string) ?? ""}
            onChange={(e) => handleChange(e.target.value)}
            placeholder={field.placeholder}
            rows={4}
            disabled={disabled}
            className={cn(inputBase, "resize-y min-h-[80px]", fieldError && "border-accent-red focus:border-accent-red")}
          />
        );

      case "number": {
        const num = typeof value === "number" ? value : Number(value);
        const minStr = field.min !== undefined ? String(field.min) : undefined;
        const maxStr = field.max !== undefined ? String(field.max) : undefined;
        const stepStr =
          field.step !== undefined ? String(field.step) : undefined;
        return (
          <div className="flex items-center gap-2">
            <input
              type="number"
              value={typeof value === "number" ? value : value === "" ? "" : num}
              onChange={(e) => {
                const v = e.target.value === "" ? "" : Number(e.target.value);
                handleChange(v);
              }}
              min={minStr}
              max={maxStr}
              step={stepStr}
              disabled={disabled}
              className={cn(inputBase, "w-40", fieldError && "border-accent-red focus:border-accent-red")}
            />
            {field.unit && (
              <span className="text-xs text-ink-tertiary">{field.unit}</span>
            )}
            {field.min !== undefined && field.max !== undefined && (
              <input
                type="range"
                min={field.min}
                max={field.max}
                step={field.step ?? 1}
                value={num}
                onChange={(e) => handleChange(Number(e.target.value))}
                disabled={disabled}
                className="flex-1 h-1.5 rounded-full appearance-none bg-black/[0.06] accent-accent-blue cursor-pointer disabled:opacity-40"
              />
            )}
          </div>
        );
      }

      case "boolean":
        return (
          <button
            type="button"
            onClick={() => handleChange(!value)}
            disabled={disabled}
            className={cn(
              "relative w-11 h-6 rounded-full transition-all duration-200 ease-smooth",
              value
                ? "bg-accent-blue"
                : "bg-black/[0.08]",
              disabled && "opacity-40 cursor-not-allowed"
            )}
          >
            <span
              className={cn(
                "absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow-sm transition-transform duration-200 ease-bounce",
                value ? "translate-x-5" : "translate-x-0"
              )}
            />
          </button>
        );

      case "choice":
        return (
          <select
            value={String(value)}
            onChange={(e) => handleChange(e.target.value)}
            disabled={disabled}
            className={cn(inputBase, "appearance-none bg-[url('data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMiIgaGVpZ2h0PSIxMiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9IiM5Q0EzQUYiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIj48cG9seWxpbmUgcG9pbnRzPSI2IDkgMTIgMTUgMTggOSI+PC9wb2x5bGluZT48L3N2Zz4=')] bg-no-repeat bg-[center_right_10px]", fieldError && "border-accent-red focus:border-accent-red")}
            style={{ paddingRight: 30 }}
          >
            {field.choices?.map((c) => (
              <option key={c} value={c}>
                {field.choiceLabels?.[c] ?? c}
              </option>
            ))}
          </select>
        );

      case "string_list": {
        const list = Array.isArray(value) ? (value as string[]) : [];
        return (
          <div className="space-y-1.5">
            {list.map((item, i) => (
              <div key={i} className="flex items-center gap-2">
                <input
                  type="text"
                  value={item}
                  onChange={(e) => {
                    const next = [...list];
                    next[i] = e.target.value;
                    handleChange(next);
                  }}
                  disabled={disabled}
                  className={cn(inputBase, "flex-1")}
                />
                <button
                  type="button"
                  onClick={() => {
                    const next = list.filter((_, idx) => idx !== i);
                    handleChange(next);
                  }}
                  disabled={disabled}
                  className="px-2 py-1.5 text-xs text-accent-red hover:bg-accent-red/5 rounded-lg transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  删除
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() => handleChange([...list, ""])}
              disabled={disabled}
              className="text-xs text-accent-blue hover:text-accent-blue-dark font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              + 添加一项
            </button>
          </div>
        );
      }

      default:
        return (
          <input
            type="text"
            value={String(value ?? "")}
            onChange={(e) => handleChange(e.target.value)}
            disabled={disabled}
            className={cn(inputBase, fieldError && "border-accent-red focus:border-accent-red")}
          />
        );
    }
  };

  const shouldShow = !field.advanced || showAdvanced;

  if (!shouldShow) return null;

  const isHighRisk = field.riskLevel === "high" || field.riskLevel === "critical";

  return (
    <div
      id={`field-${field.key}`}
      className={cn(
        "group relative rounded-2xl border px-4 py-3.5 transition-all duration-150",
        modified
          ? "bg-accent-blue/[0.02] border-accent-blue/15"
          : "bg-card border-border hover:border-border-hover",
        isHighRisk && "border-accent-red/20"
      )}
    >
      <div className="flex items-start justify-between mb-2">
        <div className="flex items-center gap-1.5">
          <label className={cn("text-sm font-medium", editable ? "text-ink" : "text-ink-secondary")}>{field.label}</label>
          {!baseEditable && (
            <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md bg-black/[0.04] text-ink-tertiary text-[10px] font-medium border border-border">
              <Lock size={9} />
              只读
            </span>
          )}
          {baseEditable && needsUnlock && !fieldUnlocked && (
            <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md bg-black/[0.04] text-ink-tertiary text-[10px] font-medium border border-border">
              <Lock size={9} />
              默认保护
            </span>
          )}
          <RiskBadge level={field.riskLevel} warning={field.riskWarning} />
          {modified && editable && (
            <span className="w-1.5 h-1.5 rounded-full bg-accent-blue animate-pulse"></span>
          )}
          {field.restartRequired && (
            <span className="px-1.5 py-0.5 rounded-md bg-accent-orange/10 text-accent-orange text-[10px] font-medium">
              需重启
            </span>
          )}
        </div>
        <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            type="button"
            onClick={() => setTooltipOpen(!tooltipOpen)}
            aria-label="查看说明"
            className="p-1 rounded-md text-ink-tertiary hover:text-ink-secondary hover:bg-black/[0.04] transition-colors"
          >
            <HelpCircle size={13} />
          </button>
          {baseEditable && needsUnlock && !fieldUnlocked && (
            <button
              type="button"
              onClick={() => setFieldUnlocked(true)}
              className="px-2 py-1 rounded-md text-[10px] font-medium text-accent-blue hover:bg-accent-blue/5 transition-colors"
            >
              启用修改
            </button>
          )}
          {modified && baseEditable && (
            <button
              type="button"
              onClick={() => resetField(field.key)}
              aria-label="重置为默认值"
              className="p-1 rounded-md text-ink-tertiary hover:text-ink-secondary hover:bg-black/[0.04] transition-colors"
              title="重置为默认值"
            >
              <RotateCcw size={13} />
            </button>
          )}
        </div>
      </div>

      {tooltipOpen && (
        <div className="mb-2 p-2.5 rounded-lg bg-surface border border-border text-xs text-ink-secondary leading-relaxed">
          <p>{field.description}</p>
          {field.source && (
            <p className="mt-1 text-[11px] text-ink-tertiary">来源：{field.source}</p>
          )}
        </div>
      )}

      {baseEditable && needsUnlock && !fieldUnlocked && (
        <div className="mb-2 rounded-lg border border-border bg-black/[0.02] px-2.5 py-2 text-[11px] text-ink-tertiary">
          该字段默认灰色保护，避免误改子代理、能力路由或安全分析策略。确认理解影响后可点击“启用修改”。
        </div>
      )}

      <div className="mb-1">{renderControl()}</div>

      {fieldError && (
        <div className="mt-1.5 flex items-start gap-1.5 text-[11px] text-accent-red/80 leading-relaxed">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />
          {fieldError}
        </div>
      )}

      {isHighRisk && field.riskWarning && (
        <div className="mt-1.5 flex items-start gap-1.5 text-[11px] text-accent-red/80 leading-relaxed">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />
          {field.riskWarning}
        </div>
      )}
    </div>
  );
}

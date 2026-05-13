import { useAuthStore } from "../../stores/authStore";
import { ReadOnlyMask } from "./PermissionGate";
import { cn } from "../../lib/utils";

// ---------------------------------------------------------------------------
// Section Title
// ---------------------------------------------------------------------------
export function SectionTitle({
  icon: Icon,
  title,
  subtitle,
}: {
  icon: React.ElementType;
  title: string;
  subtitle?: string;
}) {
  return (
    <div className="flex items-center gap-3 mb-4">
      <div className="w-8 h-8 rounded-xl bg-accent-blue/10 flex items-center justify-center">
        <Icon size={16} className="text-accent-blue" />
      </div>
      <div>
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {subtitle && (
          <p className="text-[11px] text-ink-tertiary">{subtitle}</p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Number Field
// ---------------------------------------------------------------------------
export function NumberField({
  label,
  description,
  value,
  onChange,
  min,
  max,
  unit,
  disabled,
  error,
}: {
  label: string;
  description?: string;
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  unit?: string;
  disabled?: boolean;
  error?: string;
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-xs font-medium text-ink-secondary">{label}</label>
      {description && (
        <p className="text-[10px] text-ink-tertiary leading-relaxed">{description}</p>
      )}
      <div className="flex items-center gap-2">
        <input
          type="number"
          value={value}
          onChange={(e) => {
            const v = e.target.value === "" ? 0 : Number(e.target.value);
            onChange(v);
          }}
          min={min}
          max={max}
          disabled={disabled}
          className={cn(
            "w-28 px-3 py-2 text-sm bg-surface border border-border rounded-lg text-ink focus:outline-none focus:border-accent-blue/40 transition-all",
            disabled && "bg-black/[0.02] text-ink-tertiary cursor-not-allowed",
            error && "border-accent-red focus:border-accent-red"
          )}
        />
        {unit && <span className="text-xs text-ink-tertiary">{unit}</span>}
      </div>
      {error && (
        <p className="text-[11px] text-accent-red">{error}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// String Field
// ---------------------------------------------------------------------------
export function StringField({
  label,
  description,
  value,
  onChange,
  placeholder,
  disabled,
  error,
}: {
  label: string;
  description?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
  error?: string;
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-xs font-medium text-ink-secondary">{label}</label>
      {description && (
        <p className="text-[10px] text-ink-tertiary leading-relaxed">{description}</p>
      )}
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className={cn(
          "w-full max-w-sm px-3 py-2 text-sm bg-surface border border-border rounded-lg text-ink focus:outline-none focus:border-accent-blue/40 transition-all",
          disabled && "bg-black/[0.02] text-ink-tertiary cursor-not-allowed",
          error && "border-accent-red focus:border-accent-red"
        )}
      />
      {error && (
        <p className="text-[11px] text-accent-red">{error}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Choice Field
// ---------------------------------------------------------------------------
export function ChoiceField({
  label,
  description,
  value,
  choices,
  onChange,
  disabled,
}: {
  label: string;
  description?: string;
  value: string;
  choices: string[];
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-xs font-medium text-ink-secondary">{label}</label>
      {description && (
        <p className="text-[10px] text-ink-tertiary leading-relaxed">{description}</p>
      )}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className={cn(
          "w-full max-w-xs px-3 py-2 text-sm bg-surface border border-border rounded-lg text-ink focus:outline-none focus:border-accent-blue/40 transition-all appearance-none",
          "bg-[url('data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMiIgaGVpZ2h0PSIxMiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9IiM5Q0EzQUYiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIj48cG9seWxpbmUgcG9pbnRzPSI2IDkgMTIgMTUgMTggOSI+PC9wb2x5bGluZT48L3N2Zz4=')] bg-no-repeat bg-[center_right_10px]",
          disabled && "bg-black/[0.02] text-ink-tertiary cursor-not-allowed"
        )}
        style={{ paddingRight: 30 }}
      >
        {choices.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Toggle Field
// ---------------------------------------------------------------------------
export function ToggleField({
  label,
  description,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  description?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-start justify-between py-2 gap-3">
      <div>
        <span className="text-sm text-ink-secondary">{label}</span>
        {description && (
          <p className="text-[10px] text-ink-tertiary mt-0.5 leading-relaxed">{description}</p>
        )}
      </div>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn(
          "relative w-11 h-6 rounded-full transition-all duration-200 shrink-0",
          checked ? "bg-accent-blue" : "bg-black/[0.08]",
          disabled && "opacity-40 cursor-not-allowed"
        )}
      >
        <span
          className={cn(
            "absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow-sm transition-transform duration-200",
            checked ? "translate-x-5" : "translate-x-0"
          )}
        />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Divider
// ---------------------------------------------------------------------------
export function Divider() {
  return <div className="h-px bg-border my-3" />;
}

// ---------------------------------------------------------------------------
// Admin Section wrapper
// ---------------------------------------------------------------------------
export function AdminSection({
  children,
  title,
  icon,
  subtitle,
}: {
  children: React.ReactNode;
  title: string;
  icon: React.ElementType;
  subtitle?: string;
}) {
  const isAdmin = useAuthStore((s) => s.isAdmin);
  return (
    <section className="bg-card rounded-2xl border border-border p-5 shadow-card">
      <SectionTitle icon={icon} title={title} subtitle={subtitle} />
      <ReadOnlyMask visible={!isAdmin}>{children}</ReadOnlyMask>
    </section>
  );
}

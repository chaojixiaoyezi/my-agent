import { cn } from "../../lib/utils";
import { useConfigStore } from "../../stores/configStore";
import type { ConfigCategory } from "../../types/config";
import {
  Settings,
  Brain,
  Wrench,
  Database,
  Server,
  Users,
  CheckCircle,
  Shield,
} from "lucide-react";

const iconMap: Record<string, React.ElementType> = {
  Settings,
  Brain,
  Wrench,
  Database,
  Server,
  Users,
  CheckCircle,
  Shield,
};

function ConfigSidebarItem({ cat }: { cat: ConfigCategory }) {
  const activeCategory = useConfigStore((s) => s.activeCategory);
  const setActiveCategory = useConfigStore((s) => s.setActiveCategory);
  const modifiedCount = useConfigStore((s) => {
    let count = 0;
    for (const f of cat.fields) {
      if (
        JSON.stringify(s.draftValues[f.key]) !== JSON.stringify(f.defaultValue)
      ) {
        count++;
      }
    }
    return count;
  });

  const Icon = iconMap[cat.icon || "Settings"];
  const isActive = activeCategory === cat.key;

  return (
    <button
      onClick={() => setActiveCategory(cat.key)}
      className={cn(
        "w-full flex items-center gap-2.5 px-3 py-2 rounded-2xl text-sm font-medium transition-all duration-150 ease-smooth text-left",
        isActive
          ? "bg-accent-blue/10 text-accent-blue"
          : "text-ink-secondary hover:bg-black/[0.03] hover:text-ink"
      )}
    >
      {Icon && <Icon size={16} strokeWidth={1.8} />}
      <span className="flex-1 truncate">{cat.label}</span>
      {modifiedCount > 0 && (
        <span className="ml-auto px-1.5 py-0.5 rounded-full bg-accent-blue text-white text-[10px] font-semibold min-w-[18px] text-center">
          {modifiedCount}
        </span>
      )}
    </button>
  );
}

export function ConfigSidebar() {
  const categories = useConfigStore((s) => s.schema.categories);

  return (
    <div className="w-52 shrink-0">
      <div className="space-y-0.5">
        {categories.map((cat) => (
          <ConfigSidebarItem key={cat.key} cat={cat} />
        ))}
      </div>
    </div>
  );
}

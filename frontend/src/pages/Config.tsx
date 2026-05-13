import { useMemo, useState, useCallback, useEffect, useRef } from "react";
import {
  Search,
  SlidersHorizontal,
  RotateCcw,
  Save,
  FileJson,
  GitCompare,
  Loader2,
} from "lucide-react";
import { useConfigStore } from "../stores/configStore";
import { ConfigSidebar } from "../components/config/ConfigSidebar";
import { ConfigFieldRenderer } from "../components/config/ConfigFieldRenderer";
import { ConfigDiff } from "../components/config/ConfigDiff";
import { ConfigYamlModal } from "../components/config/ConfigYamlModal";
import { ConfirmDialog } from "../components/config/ConfirmDialog";
import { cn } from "../lib/utils";

export default function Config() {
  const schema = useConfigStore((s) => s.schema);
  const isLoading = useConfigStore((s) => s.isLoading);
  const loadError = useConfigStore((s) => s.loadError);
  const showAdvanced = useConfigStore((s) => s.showAdvanced);
  const searchQuery = useConfigStore((s) => s.searchQuery);
  const activeCategory = useConfigStore((s) => s.activeCategory);
  const showDiff = useConfigStore((s) => s.showDiff);
  const showYaml = useConfigStore((s) => s.showYaml);
  const toggleAdvanced = useConfigStore((s) => s.toggleAdvanced);
  const setSearchQuery = useConfigStore((s) => s.setSearchQuery);
  const setActiveCategory = useConfigStore((s) => s.setActiveCategory);
  const toggleDiff = useConfigStore((s) => s.toggleDiff);
  const toggleYaml = useConfigStore((s) => s.toggleYaml);
  const openConfirm = useConfigStore((s) => s.openConfirm);
  const showToast = useConfigStore((s) => s.showToast);
  const getModifiedCount = useConfigStore((s) => s.getModifiedCount);

  const [searchInput, setSearchInput] = useState(searchQuery);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const debouncedSearch = useCallback(
    (q: string) => {
      if (searchTimerRef.current) {
        clearTimeout(searchTimerRef.current);
      }
      searchTimerRef.current = setTimeout(() => {
        setSearchQuery(q);
      }, 300);
    },
    [setSearchQuery]
  );

  const handleSearch = (e: React.ChangeEvent<HTMLInputElement>) => {
    const q = e.target.value;
    setSearchInput(q);
    debouncedSearch(q);
  };

  useEffect(() => {
    return () => {
      if (searchTimerRef.current) {
        clearTimeout(searchTimerRef.current);
      }
    };
  }, []);

  const filteredCategories = useMemo(() => {
    if (!searchQuery.trim()) {
      return schema.categories;
    }
    const q = searchQuery.toLowerCase();
    return schema.categories
      .map((cat) => ({
        ...cat,
        fields: cat.fields.filter(
          (f) =>
            f.key.toLowerCase().includes(q) ||
            f.label.toLowerCase().includes(q) ||
            f.description.toLowerCase().includes(q)
        ),
      }))
      .filter((cat) => cat.fields.length > 0);
  }, [schema, searchQuery]);

  const activeCat =
    filteredCategories.find((c) => c.key === activeCategory) ||
    filteredCategories[0];

  const modifiedCount = getModifiedCount();

  const visibleFields = useMemo(() => {
    if (!activeCat) return [];
    return activeCat.fields.filter(
      (f) => showAdvanced || !f.advanced
    );
  }, [activeCat, showAdvanced]);

  const handleSave = () => {
    const { draftValues } = useConfigStore.getState();
    const highRiskFields: string[] = [];
    for (const cat of schema.categories) {
      for (const f of cat.fields) {
        if (
          (f.riskLevel === "high" || f.riskLevel === "critical") &&
          f.confirmationRequired
        ) {
          const val = draftValues[f.key];
          const orig = f.defaultValue;
          if (JSON.stringify(val) !== JSON.stringify(orig)) {
            highRiskFields.push(f.key);
          }
        }
      }
    }
    if (highRiskFields.length > 0) {
      const f = schema.categories
        .flatMap((c) => c.fields)
        .find((f) => f.key === highRiskFields[0]);
      if (f) {
        openConfirm(f.key, f.label, f.riskWarning || "");
        return;
      }
    }
    showToast("配置已保存（mock）", "success");
  };

  // Update active category if filtered results change
  useEffect(() => {
    if (
      activeCat &&
      !filteredCategories.some((c) => c.key === activeCategory)
    ) {
      setActiveCategory(filteredCategories[0]?.key || "basic");
    }
  }, [filteredCategories, activeCategory, activeCat, setActiveCategory]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full text-ink-tertiary text-sm">
        <Loader2 size={18} className="animate-spin mr-2" />
        加载配置中...
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="flex items-center justify-center h-full text-accent-red text-sm">
        加载失败：{loadError}
      </div>
    );
  }

  return (
    <div className="flex gap-5 h-full">
      <ConfigSidebar />

      <div className="flex-1 min-w-0 flex flex-col">
        {/* Toolbar */}
        <div className="flex items-center justify-between gap-3 mb-4">
          <div className="relative flex-1 max-w-md">
            <Search
              size={14}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-tertiary"
            />
            <input
              type="text"
              value={searchInput}
              onChange={handleSearch}
              placeholder="搜索配置项..."
              className="w-full pl-9 pr-3 py-2 text-sm bg-card border border-border rounded-xl text-ink placeholder:text-ink-tertiary focus:outline-none focus:border-accent-blue/40 focus:shadow-focus transition-all duration-150"
            />
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={toggleAdvanced}
              className={cn(
                "flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium border transition-all duration-150",
                showAdvanced
                  ? "bg-accent-blue/10 border-accent-blue/20 text-accent-blue"
                  : "bg-card border-border text-ink-secondary hover:border-border-hover"
              )}
            >
              <SlidersHorizontal size={13} />
              {showAdvanced ? "高级已开" : "高级"}
            </button>

            <button
              onClick={toggleDiff}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium border bg-card border-border text-ink-secondary hover:border-border-hover transition-all duration-150"
            >
              <GitCompare size={13} />
              对比
              {modifiedCount > 0 && (
                <span className="ml-0.5 px-1.5 py-0 rounded-full bg-accent-blue text-white text-[10px]">
                  {modifiedCount}
                </span>
              )}
            </button>

            <button
              onClick={toggleYaml}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium border bg-card border-border text-ink-secondary hover:border-border-hover transition-all duration-150"
            >
              <FileJson size={13} />
              导出
            </button>

            <button
              onClick={() => useConfigStore.getState().resetAll()}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium border bg-card border-border text-ink-secondary hover:border-border-hover transition-all duration-150"
            >
              <RotateCcw size={13} />
              恢复默认
            </button>

            <button
              onClick={handleSave}
              className={cn(
                "flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-medium text-white transition-all duration-150 active:scale-[0.97]",
                modifiedCount > 0
                  ? "bg-accent-blue hover:bg-accent-blue-dark shadow-card hover:shadow-card-hover hover:-translate-y-px"
                  : "bg-accent-blue/40 cursor-not-allowed"
              )}
              disabled={modifiedCount === 0}
            >
              <Save size={13} />
              保存
            </button>
          </div>
        </div>

        {/* Category title */}
        {activeCat && (
          <div className="mb-3">
            <h2 className="text-sm font-semibold text-ink">{activeCat.label}</h2>
            <p className="text-xs text-ink-tertiary mt-0.5">{activeCat.description}</p>
          </div>
        )}

        {/* Fields */}
        <div className="space-y-3 overflow-y-auto flex-1 pr-1">
          {visibleFields.map((field) => (
            <ConfigFieldRenderer key={field.key} field={field} />
          ))}
          {visibleFields.length === 0 && (
            <div className="text-center py-12 text-ink-tertiary text-sm">
              该分类下没有{showAdvanced ? "" : "基础"}配置项
            </div>
          )}
        </div>
      </div>

      {showDiff && <ConfigDiff />}
      {showYaml && <ConfigYamlModal />}
      <ConfirmDialog />
    </div>
  );
}

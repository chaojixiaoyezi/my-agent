import { useState, useCallback } from "react";
import { useConfigStore } from "../../stores/configStore";

export function useSettingsSection(sectionName: string) {
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const showToast = useConfigStore((s) => s.showToast);

  const markDirty = useCallback(() => {
    setDirty(true);
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    await new Promise((r) => setTimeout(r, 800));
    setSaving(false);
    setDirty(false);
    showToast(`${sectionName} 已保存（mock）`, "success");
  }, [sectionName, showToast]);

  return { dirty, saving, markDirty, handleSave };
}

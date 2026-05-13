import { useSettingsStore } from "../../stores/settingsStore";
import { useAuthStore } from "../../stores/authStore";
import {
  AdminSection,
  NumberField,
  StringField,
  ToggleField,
} from "../../components/settings/SettingsFieldComponents";
import { SettingsPageHeader } from "../../components/settings/SettingsPageHeader";
import { useSettingsSection } from "../../components/settings/useSettingsSection";
import { useDirtyGuard } from "../../components/settings/useDirtyGuard";
import { Brain } from "lucide-react";

export default function SettingsModel() {
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const model = useSettingsStore((s) => s.modelParams);
  const setModel = useSettingsStore((s) => s.setModelParams);
  const errors = useSettingsStore((s) => s.errors);
  const { dirty, saving, markDirty, handleSave } = useSettingsSection("模型参数");
  useDirtyGuard(dirty);

  return (
    <div className="space-y-6 max-w-3xl">
      <SettingsPageHeader title="模型参数（Model）" subtitle="LLM 采样参数、后端配置与性能调优" saving={saving} onSave={handleSave} dirty={dirty} />

      <AdminSection
        icon={Brain}
        title="采样参数（Sampling）"
        subtitle="控制 LLM 生成行为的温度、采样和 token 限制"
      >
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <NumberField
            label="temperature（温度）"
            description="采样随机度，0 最确定，2 最随机"
            value={model.temperature}
            onChange={(v) => { setModel({ temperature: v }); markDirty(); }}
            min={0}
            max={2}
            unit=""
            disabled={!isAdmin}
            error={errors["mp_temp"]}
          />
          <NumberField
            label="top_p（核采样阈值）"
            description="nucleus sampling 概率累积阈值"
            value={model.top_p}
            onChange={(v) => { setModel({ top_p: v }); markDirty(); }}
            min={0}
            max={1}
            unit=""
            disabled={!isAdmin}
          />
          <NumberField
            label="max_tokens（最大 token 数）"
            description="模型单次返回的最大 token 数"
            value={model.max_tokens}
            onChange={(v) => { setModel({ max_tokens: v }); markDirty(); }}
            min={256}
            max={128000}
            unit="tokens"
            disabled={!isAdmin}
            error={errors["mp_tokens"]}
          />
          <NumberField
            label="timeout（请求超时）"
            description="单次模型请求超时时间"
            value={model.timeout}
            onChange={(v) => { setModel({ timeout: v }); markDirty(); }}
            min={5}
            max={600}
            unit="秒"
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>

      <AdminSection
        icon={Brain}
        title="后端配置（Backend）"
        subtitle="API 版本、密钥环境变量与性能分析"
      >
        <div className="grid grid-cols-2 gap-4">
          <StringField
            label="anthropic_version（API 版本）"
            description="Anthropic API 版本号"
            value={model.anthropic_version}
            onChange={(v) => { setModel({ anthropic_version: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <StringField
            label="api_key_env（密钥环境变量名）"
            description="读取 API Key 的环境变量名称"
            value={model.api_key_env}
            onChange={(v) => { setModel({ api_key_env: v }); markDirty(); }}
            disabled={!isAdmin}
          />
          <StringField
            label="model_speed_profile_path（速度分析路径）"
            description="模型速度分析文件路径"
            value={model.model_speed_profile_path}
            onChange={(v) => { setModel({ model_speed_profile_path: v }); markDirty(); }}
            placeholder="留空表示禁用"
            disabled={!isAdmin}
          />
          <ToggleField
            label="auto_bench_model_on_first_use（首次使用基准测试）"
            description="首次调用模型时是否自动执行速度基准测试"
            checked={model.auto_bench_model_on_first_use}
            onChange={(v) => { setModel({ auto_bench_model_on_first_use: v }); markDirty(); }}
            disabled={!isAdmin}
          />
        </div>
      </AdminSection>
    </div>
  );
}

export type ConfigFieldType =
  | "string"
  | "number"
  | "boolean"
  | "choice"
  | "string_list"
  | "path"
  | "secret"
  | "json"
  | "duration_seconds"
  | "text";

export type RiskLevel = "low" | "medium" | "high" | "critical";

export type ConfigField = {
  key: string;
  label: string;
  description: string;
  shortDescription?: string;
  type: ConfigFieldType;
  defaultValue: unknown;
  currentValue?: unknown;
  min?: number;
  max?: number;
  minLength?: number;
  maxLength?: number;
  choices?: string[];
  choiceLabels?: Record<string, string>;
  pattern?: string;
  patternMessage?: string;
  placeholder?: string;
  category: string;
  categoryLabel?: string;
  subCategory?: string;
  advanced?: boolean;
  order?: number;
  restartRequired?: boolean;
  riskLevel?: RiskLevel;
  riskWarning?: string;
  confirmationRequired?: boolean;
  dependsOn?: { field: string; value: unknown };
  mutuallyExclusiveWith?: string[];
  envVar?: string;
  envOverride?: boolean;
  unit?: string;
  step?: number;
  docLink?: string;
  versionAdded?: string;
  source?: string;
  requiresUnlock?: boolean;
};

export type ConfigCategory = {
  key: string;
  label: string;
  description: string;
  icon?: string;
  order: number;
  fields: ConfigField[];
};

export type ConfigSchema = {
  version: string;
  categories: ConfigCategory[];
  meta: {
    lastUpdated: string;
    source: string;
    editable: boolean;
    exportFormats: ("yaml" | "json")[];
  };
};

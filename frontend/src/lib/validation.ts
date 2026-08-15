const DANGEROUS_CHARS = new Set([
  ";",
  "|",
  "&",
  "$",
  "`",
  "\n",
  "\r",
  String.fromCharCode(0),
  "<",
  ">",
]);

function hasDangerousChars(value: string): boolean {
  for (const char of value) {
    if (DANGEROUS_CHARS.has(char)) {
      return true;
    }
  }
  return false;
}

const PATH_PATTERN = /^[a-zA-Z0-9_./~-]+$/;

const URL_PATTERN =
  /^https?:\/\/(?:[\w-]+\.)*[\w-]+(?::\d+)?(?:\/[\w.~%!$&'()*+,;=:@/-]*)*$/;

export type ValidationResult = { valid: boolean; error?: string };

export function validateString(
  value: string,
  opts: {
    maxLength?: number;
    minLength?: number;
    allowEmpty?: boolean;
    label?: string;
  } = {}
): ValidationResult {
  const { maxLength = 2048, minLength = 0, allowEmpty = true, label = "输入" } = opts;
  if (!allowEmpty && value.trim().length === 0) {
    return { valid: false, error: `${label}不能为空` };
  }
  if (value.length > maxLength) {
    return { valid: false, error: `${label}最多 ${maxLength} 个字符` };
  }
  if (value.length < minLength) {
    return { valid: false, error: `${label}最少 ${minLength} 个字符` };
  }
  if (hasDangerousChars(value)) {
    return {
      valid: false,
      error: `${label}包含非法字符，禁止使用 ; | & $ \u0060 换行等`,
    };
  }
  return { valid: true };
}

export function validatePath(value: string): ValidationResult {
  if (value.trim().length === 0) {
    return { valid: true }; // 允许空路径（使用默认值）
  }
  if (value.length > 512) {
    return { valid: false, error: "路径最多 512 个字符" };
  }
  if (hasDangerousChars(value)) {
    return {
      valid: false,
      error: "路径包含非法字符，禁止使用 ; | & $ \u0060 换行等",
    };
  }
  if (!PATH_PATTERN.test(value)) {
    return {
      valid: false,
      error: "路径只能包含字母、数字、下划线、横线、斜杠、点和波浪号",
    };
  }
  // 防止路径遍历
  if (value.includes("..") || value.includes("//")) {
    return { valid: false, error: "路径不能包含 .. 或连续的 /" };
  }
  return { valid: true };
}

export function validateUrl(value: string): ValidationResult {
  if (value.trim().length === 0) {
    return { valid: false, error: "URL 不能为空" };
  }
  if (value.length > 1024) {
    return { valid: false, error: "URL 最多 1024 个字符" };
  }
  if (!URL_PATTERN.test(value)) {
    return { valid: false, error: "URL 格式不正确" };
  }
  return { valid: true };
}

export function validateNumber(
  value: number,
  opts: { min?: number; max?: number; label?: string } = {}
): ValidationResult {
  const { min, max, label = "数值" } = opts;
  if (Number.isNaN(value)) {
    return { valid: false, error: `${label}必须是有效数字` };
  }
  if (min !== undefined && value < min) {
    return { valid: false, error: `${label}最小值为 ${min}` };
  }
  if (max !== undefined && value > max) {
    return { valid: false, error: `${label}最大值为 ${max}` };
  }
  return { valid: true };
}

export function escapeHtml(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

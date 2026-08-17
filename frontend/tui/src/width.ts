/**
 * Unicode 显示宽度（未完成项②）：终端列宽对齐与断行的唯一事实来源。
 * 规则（终端渲染惯例）：
 *   - 全角/宽字符（East Asian Width W/F：CJK 表意、假名、谚文、CJK 符号、
 *     全角标点）→ 2 列
 *   - 常见 emoji（杂项符号 2600-27BF / 装饰 1F300-1FAFF / 区域旗等）→ 2 列
 *   - 零宽字符（ZWJ、BOM、方向控制、组合标记、variation selector）→ 0 列
 *   - 其余 → 1 列
 * 不用 string-width 依赖（体积/可控性），规则收敛到本模块并单测锁定。
 */

/** 码点是否全角（East Asian Width W/F 常用区段） */
function isWideCodePoint(cp: number): boolean {
  return (
    (cp >= 0x1100 && cp <= 0x115f) || // Hangul Jamo
    (cp >= 0x2e80 && cp <= 0x303e) || // CJK Radicals .. CJK Symbols
    (cp >= 0x3041 && cp <= 0x33ff) || // Hiragana .. CJK Compatibility
    (cp >= 0x3400 && cp <= 0x4dbf) || // CJK Ext A
    (cp >= 0x4e00 && cp <= 0x9fff) || // CJK Unified
    (cp >= 0xa000 && cp <= 0xa4cf) || // Yi
    (cp >= 0xac00 && cp <= 0xd7a3) || // Hangul Syllables
    (cp >= 0xf900 && cp <= 0xfaff) || // CJK Compatibility Ideographs
    (cp >= 0xfe30 && cp <= 0xfe4f) || // CJK Compat Forms
    (cp >= 0xff00 && cp <= 0xff60) || // Fullwidth Forms
    (cp >= 0xffe0 && cp <= 0xffe6) || // Fullwidth Signs
    (cp >= 0x1f300 && cp <= 0x1faff) || // emoji / 装饰符号 / 区域旗
    (cp >= 0x20000 && cp <= 0x2fffd) || // CJK Ext B..
    (cp >= 0x30000 && cp <= 0x3fffd)
  );
}

/** 码点是否零宽（组合标记/ZWJ/方向控制/BOM/variation selector/肤色修饰符） */
function isZeroWidthCodePoint(cp: number): boolean {
  return (
    (cp >= 0x0300 && cp <= 0x036f) || // combining diacritics
    (cp >= 0x1ab0 && cp <= 0x1aff) ||
    (cp >= 0x1dc0 && cp <= 0x1dff) ||
    (cp >= 0x20d0 && cp <= 0x20ff) || // combining marks
    (cp >= 0xfe20 && cp <= 0xfe2f) ||
    (cp >= 0x200b && cp <= 0x200f) || // zero width space / joiner / direction
    cp === 0x202a || cp === 0x202b || cp === 0x202c || cp === 0x202d || cp === 0x202e || // bidi
    cp === 0x2060 || cp === 0xfeff || // word joiner / BOM
    cp === 0xfe0e || cp === 0xfe0f || // variation selectors
    (cp >= 0xfe00 && cp <= 0xfe0d) || // variation selectors range
    (cp >= 0x1f3fb && cp <= 0x1f3ff) || // emoji skin tone modifiers（叠加在基字符上不占列）
    cp === 0x200d // ZWJ
  );
}

/** 单个码点的显示宽度（列数） */
export function codePointWidth(cp: number): number {
  if (isZeroWidthCodePoint(cp)) return 0;
  if (isWideCodePoint(cp)) return 2;
  return 1;
}

/** 字符串的显示宽度（终端列数） */
export function displayWidth(text: string): number {
  let width = 0;
  for (const ch of text) {
    width += codePointWidth(ch.codePointAt(0) ?? 0);
  }
  return width;
}

/**
 * 按显示宽度截断（尾部省略号计入宽度）。
 * 返回 { text, truncated }：truncated=true 表示被截断。
 */
export function truncateByWidth(text: string, maxWidth: number): { text: string; truncated: boolean } {
  const ELLIPSIS = "…"; // 宽 1 列
  if (displayWidth(text) <= maxWidth) return { text, truncated: false };
  let width = 0;
  let out = "";
  for (const ch of text) {
    const w = codePointWidth(ch.codePointAt(0) ?? 0);
    if (width + w + 1 > maxWidth) break; // 预留省略号 1 列
    out += ch;
    width += w;
  }
  return { text: out + ELLIPSIS, truncated: true };
}

/**
 * 按显示宽度断行（不拆词，空格处优先；无空格则硬断）。
 * 用于 markdown 行数估算/对齐（虚拟滚动行模型依赖）。
 */
export function wrapByWidth(text: string, maxWidth: number): string[] {
  if (maxWidth <= 0) return text.length === 0 ? [""] : text.split("\n");
  const lines: string[] = [];
  for (const rawLine of text.split("\n")) {
    if (displayWidth(rawLine) <= maxWidth) {
      lines.push(rawLine);
      continue;
    }
    let current = "";
    let currentWidth = 0;
    for (const ch of rawLine) {
      const w = codePointWidth(ch.codePointAt(0) ?? 0);
      if (currentWidth + w > maxWidth) {
        lines.push(current);
        current = "";
        currentWidth = 0;
      }
      current += ch;
      currentWidth += w;
    }
    lines.push(current);
  }
  return lines;
}

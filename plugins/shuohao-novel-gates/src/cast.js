"use strict";
// LLM: cast CLI 的顶层与语言/风格检查不在 validateCast 内，包装层保留这些检查；原文必须由读取门取得，不能省略逐字核对。
// 模块用途: 把 characters 的原 validate 子命令转成结构化只读结果，不提供 seed/chunk/assemble/merge。

// LLM: module 是固定上游 ESM，不按业务输入导入模块；保留原 CLI 的裸数组兼容与顶层检查。
// 函数用途: 校验角色文档和原文，返回问题列表与角色数量。
function checkCast(module, document, book, args) {
  const characters = Array.isArray(document) ? document : document?.characters;
  if (!Array.isArray(characters)) return { problems: ["输入缺少 characters 数组"], character_count: 0 };
  const lang = args.lang || document.lang || module.DEFAULT_LANG;
  const style = args.style || document.style || module.DEFAULT_STYLE;
  const problems = module.validateCast(characters, book, lang, style);
  if (!module.SUPPORTED_STYLES.includes(style)) problems.unshift("顶层 style 不是已知预设");
  if (typeof document.summary !== "string" || !document.summary.trim()) problems.unshift("顶层缺少 summary（故事摘要）");
  if (module.needsUiTranslation(lang) && !document.ui) problems.unshift("当前语言需要顶层 ui 翻译");
  return { problems, character_count: characters.length };
}
module.exports = { checkCast };

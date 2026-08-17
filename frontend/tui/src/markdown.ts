/**
 * markdown → 终端渲染块（2026-08-17 完整版补齐：助手回复 markdown 渲染）。
 * markdown-it 解析 → 结构化块（heading/code/text/list/quote/hr）+
 * 内联样式分段（**bold** / `code` / [link](url) 显示为链接色）。
 */

import MarkdownIt from "markdown-it";

const md = new MarkdownIt({ html: false, linkify: false, breaks: false });

export interface InlineSegment {
  text: string;
  bold?: boolean;
  code?: boolean;
  link?: boolean;
}

export interface MdBlock {
  kind: "heading" | "code" | "text" | "list" | "quote" | "hr" | "empty";
  text: string;
  level?: number;
  /** 内联分段（text/heading/list/quote 用） */
  segments?: InlineSegment[];
}

/** 内联解析：**bold** / `code` / [text](url) */
export function inlineSegments(text: string): InlineSegment[] {
  const segments: InlineSegment[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    const idx = m.index ?? 0;
    if (idx > last) segments.push({ text: text.slice(last, idx) });
    const token = m[0];
    if (token.startsWith("**")) {
      segments.push({ text: token.slice(2, -2), bold: true });
    } else if (token.startsWith("`")) {
      segments.push({ text: token.slice(1, -1), code: true });
    } else {
      const linkText = token.match(/\[([^\]]+)\]/)?.[1] ?? token;
      segments.push({ text: linkText, link: true });
    }
    last = idx + token.length;
  }
  if (last < text.length) segments.push({ text: text.slice(last) });
  return segments.length > 0 ? segments : [{ text }];
}

/** markdown 文本 → 渲染块（容器跟踪：heading/list/quote 的 inline 文本归位） */
export function markdownToBlocks(text: string): MdBlock[] {
  const blocks: MdBlock[] = [];
  const tokens = md.parse(text, {});
  let codeBuffer: string[] = [];
  // 容器跟踪：当前打开的 heading/list_item/quote 会把下一个 inline 归入
  let pendingHeading: number | null = null;
  let pendingList = false;
  let pendingQuote = false;

  const flushCode = () => {
    if (codeBuffer.length > 0) {
      blocks.push({ kind: "code", text: codeBuffer.join("\n") });
      codeBuffer = [];
    }
  };

  for (const token of tokens) {
    switch (token.type) {
      case "fence":
      case "code_block":
        codeBuffer.push(token.content);
        break;
      case "inline": {
        flushCode();
        const content = token.content.trim();
        if (!content) break;
        const segments = inlineSegments(content);
        if (pendingHeading !== null) {
          blocks.push({ kind: "heading", text: content, level: pendingHeading, segments });
          pendingHeading = null;
        } else if (pendingList) {
          blocks.push({ kind: "list", text: content, segments });
          pendingList = false;
        } else if (pendingQuote) {
          blocks.push({ kind: "quote", text: content, segments });
          pendingQuote = false;
        } else {
          blocks.push({ kind: "text", text: content, segments });
        }
        break;
      }
      case "heading_open": {
        flushCode();
        pendingHeading = Number(token.tag?.slice(1) ?? 1);
        break;
      }
      case "list_item_open": {
        flushCode();
        pendingList = true;
        break;
      }
      case "blockquote_open":
        flushCode();
        pendingQuote = true;
        break;
      case "hr":
        flushCode();
        blocks.push({ kind: "hr", text: "" });
        break;
      case "hardbreak":
        flushCode();
        blocks.push({ kind: "empty", text: "" });
        break;
      default:
        break;
    }
  }
  flushCode();
  return blocks;
}

/** 面向渲染的简化：合并相邻同类块 + heading/list 文本归位 */
export function markdownToLines(text: string): MdBlock[] {
  const raw = markdownToBlocks(text);
  const out: MdBlock[] = [];
  let pendingKind: MdBlock["kind"] | null = null;
  let pending: MdBlock | null = null;
  for (const block of raw) {
    if (block.kind === "text") {
      if (pending && pending.kind === "text") {
        pending.text += block.text;
        pending.segments = [...(pending.segments ?? []), ...(block.segments ?? [])];
      } else {
        pending = { kind: "text", text: block.text, segments: block.segments };
        pendingKind = "text";
        out.push(pending);
      }
      continue;
    }
    pending = null;
    pendingKind = null;
    out.push(block);
  }
  return out;
}

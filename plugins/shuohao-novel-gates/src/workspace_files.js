"use strict";
// LLM: 工作区读取只能来自本次 _meta 的 WorkspaceReadContext；复用 hello-node 的裁决与 no-follow 打开后 dev/ino 复核。
//   不调用上游 readJson/loadRecipes，因此参考文件和卡片没有绕过路径门的隐式读取。联测跨语言向量与 MCP 越界用例。
// 模块用途: 为五阶段门提供完整 UTF-8 输入；只读，不写用户目录或插件私有目录。
const fs = require("node:fs");
const { check, realpathLoose } = require("./workspace_read.js");
const { ToolError } = require("./errors.js");
const MAX_FILE_BYTES = 16 * 1024 * 1024;

// LLM: Node 缺少 openat，沿样例重新解析真实目标并比较打开描述符；此复核不承诺 Python SDK 的逐段目录竞态强度。
// 函数用途: 拒绝被替换的链接、目录、FIFO 和过大的输入。
function verifyOpened(fd, target) {
  const opened = fs.fstatSync(fd, { bigint: true });
  if (!opened.isFile()) throw new ToolError("NOT_A_FILE", "目标不是普通文件。");
  if (opened.size > BigInt(MAX_FILE_BYTES)) throw new ToolError("FILE_TOO_LARGE", "输入超过 16 MiB，请先分卷。");
  const current = realpathLoose(target) === target ? fs.statSync(target, { bigint: true }) : null;
  if (!current || current.dev !== opened.dev || current.ino !== opened.ino) {
    throw new ToolError("PATH_CHANGED", "文件在检查后发生变化，请重试。");
  }
  return Number(opened.size);
}

// LLM: 读取长度由已打开普通文件 stat 固定；完整解码，不能把截断内容当完整门输入。读时膨胀不造成无限循环。
// 函数用途: 从已打开的文件描述符取得有界完整文本。
function decodeFile(fd, size) {
  const buffer = Buffer.alloc(size);
  let offset = 0;
  while (offset < size) {
    const count = fs.readSync(fd, buffer, offset, size - offset, offset);
    if (!count) throw new ToolError("PATH_CHANGED", "文件读取期间被截断，请重试。");
    offset += count;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buffer);
  } catch {
    throw new ToolError("NOT_UTF8", "文件不是完整 UTF-8 文本。");
  }
}

// LLM: 每个主输入、参考输入、日志和卡片都走同一个入口；缺文件和 OS 拒绝显式失败，不补默认材料。
// 函数用途: 按宿主本次权限读取完整文本，始终关闭本次描述符。
function readText(context, requested) {
  const verdict = check(context, requested);
  if (!verdict.allowed) throw new ToolError(verdict.code, "宿主本次读取权限不允许访问这个路径。");
  let fd;
  try {
    fd = fs.openSync(verdict.target, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  } catch {
    throw new ToolError("READ_FAILED", "文件不存在、是链接或无法打开。");
  }
  try {
    return decodeFile(fd, verifyOpened(fd, verdict.target));
  } finally {
    fs.closeSync(fd);
  }
}

// LLM: JSON 格式失败区别于语义门失败；不从输入中的路径、URL 或其它字段自动加载任何文件。
// 函数用途: 读取一份结构化阶段输入。
function readJson(context, requested) {
  const text = readText(context, requested);
  try {
    return JSON.parse(text);
  } catch {
    throw new ToolError("INVALID_JSON", "输入不是有效 JSON。");
  }
}
module.exports = { readText, readJson };

"use strict";
// LLM: my-agent/workspace-read-context v1 的 Node.js 移植，逐条对应宿主 Python 参考实现
//   （agent_py_agent/agent/workspace_read_context.py 与 path_access_policy.py）。上下文只来自宿主逐次发来的 _meta，
//   解析失败整体拒绝；路径先按"逐段解析符号链接、遇 .. 回到已解析父目录"的方式得到真实目标，再判读取范围与 owner/凭据/危险目录。
//   改动必须重跑 plugins/sdk/conformance/workspace_read_check.json 一致性用例（pytest 会自动调用本文件的 runner）。
// 模块用途: 让 Node 插件在读工作区文件前做与宿主相同的权限裁决；这不是 OS 沙箱，插件进程本身仍以用户权限运行。

const fs = require("fs");

const EXTENSION = "my-agent/workspace-read-context";
const VERSION = "1";
const CONTEXT_FIELDS = ["version", "cwd", "read_roots", "path_policy", "granted_external_roots", "external_policy"];
const POLICY_FIELDS = ["mode", "dangerous_roots", "owner_scope_root", "agent_home_root"];
// 与宿主 _CREDENTIAL_FILENAMES 相同：这些文件常装 API key 或密码
const CREDENTIAL_FILENAMES = new Set([
  ".git-credentials", "auth.json", ".anthropic_oauth.json", ".credentials.json", ".netrc", ".pgpass",
]);

// LLM: 只表示宿主上下文格式或范围无效；调用方应整体拒绝本次调用，不能退回插件自己的默认目录。
// 类用途: 区分"上下文坏了"与"路径被拒绝"。
class ContextError extends Error {}

// LLM: 对应 Python 的 set(value) != fields：必须是普通对象且键集合完全相同。
// 函数用途: 检查对象字段集合是否精确匹配。
function exactKeys(value, fields) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === fields.length
    && fields.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

// LLM: 对应 _absolute_path：必须是规范绝对路径（无空段、. 段、.. 段和结尾斜杠），不展开 ~ 或环境变量。
// 函数用途: 校验并返回一个规范绝对路径。
function absolutePath(value) {
  if (typeof value !== "string" || !value || value.includes("\0") || !value.startsWith("/")) {
    throw new ContextError("上下文路径必须为规范绝对路径");
  }
  if (value !== "/" && value.split("/").slice(1).some((part) => part === "" || part === "." || part === "..")) {
    throw new ContextError("上下文路径必须为规范绝对路径");
  }
  return value;
}

// LLM: 对应 _path_list：空数组保留"全部拒绝"含义；不接受字符串冒充数组，重复项拒绝。
// 函数用途: 校验一组规范绝对路径。
function pathList(value) {
  if (!Array.isArray(value)) {
    throw new ContextError("上下文路径列表无效");
  }
  const result = value.map(absolutePath);
  if (new Set(result).size !== result.length) {
    throw new ContextError("上下文路径重复");
  }
  return result;
}

// LLM: 对应 _read_policy：只恢复宿主冻结的字段，不读本进程环境变量补默认值。
// 函数用途: 校验并恢复一份路径策略。
function readPolicy(value) {
  if (!exactKeys(value, POLICY_FIELDS) || !["normal", "full"].includes(value.mode)) {
    throw new ContextError("路径策略协议无效");
  }
  return {
    mode: value.mode,
    dangerousRoots: pathList(value.dangerous_roots),
    ownerScopeRoot: value.owner_scope_root === null ? null : absolutePath(value.owner_scope_root),
    agentHomeRoot: value.agent_home_root === null ? null : absolutePath(value.agent_home_root),
  };
}

// LLM: 对应 WorkspaceReadContext.from_payload：读取根必须在 cwd 内，外部授权根必须在某个读取根内，
//   外部策略不能带 owner 墙且模式与主策略相同。任何不符都整体拒绝。
// 函数用途: 从 tools/call 的 _meta 值恢复本次调用的读取上下文。
function parseContext(value) {
  if (!exactKeys(value, CONTEXT_FIELDS) || value.version !== VERSION) {
    throw new ContextError("工作区读取上下文协议无效");
  }
  const cwd = absolutePath(value.cwd);
  const readRoots = pathList(value.read_roots);
  const grantedExternalRoots = pathList(value.granted_external_roots);
  const policy = readPolicy(value.path_policy);
  const externalPolicy = readPolicy(value.external_policy);
  if (readRoots.some((root) => !within(root, cwd))
      || grantedExternalRoots.some((root) => !readRoots.some((allowed) => within(root, allowed)))
      || externalPolicy.ownerScopeRoot !== null || externalPolicy.mode !== policy.mode) {
    throw new ContextError("工作区读取上下文范围无效");
  }
  return { cwd, readRoots, policy, grantedExternalRoots, externalPolicy };
}

// LLM: 对应 pathlib 的 relative_to：按路径段比较且区分大小写，/a 不包含 /ab。
// 函数用途: 判断 path 是否等于 root 或位于 root 之下。
function within(path, root) {
  return path === root || path.startsWith(root.endsWith("/") ? root : root + "/");
}

// 函数用途: 对应 posixpath.join 的两段拼接（第二段是绝对路径时直接替换）。
function joinPath(head, tail) {
  if (tail.startsWith("/")) {
    return tail;
  }
  return !head || head.endsWith("/") ? head + tail : head + "/" + tail;
}

// 函数用途: 对应 posixpath.split：返回 [父路径, 最后一段]，根目录的父路径仍是根。
function splitPath(path) {
  const index = path.lastIndexOf("/") + 1;
  let head = path.slice(0, index);
  if (head && head !== "/".repeat(head.length)) {
    head = head.replace(/\/+$/, "");
  }
  return [head, path.slice(index)];
}

// LLM: 逐行移植 CPython posixpath._joinrealpath（strict=False）：不存在的段按字面保留，遇到符号链接就地展开，
//   .. 作用在已解析的父目录上（先解析链接再回退，而不是先按字面折叠 ..）。Node 自带的 fs.realpath 要求路径存在，
//   且会先按字面折叠 ..，两点都与宿主不同，所以不能直接用。
// 函数用途: 解析 path 的真实目标；返回 [路径, 是否完整解析]，遇到链接环返回 false。
function joinRealpath(path, rest, seen) {
  if (rest.startsWith("/")) {
    rest = rest.slice(1);
    path = "/";
  }
  while (rest) {
    const index = rest.indexOf("/");
    const name = index < 0 ? rest : rest.slice(0, index);
    rest = index < 0 ? "" : rest.slice(index + 1);
    if (!name || name === ".") {
      continue;
    }
    if (name === "..") {
      if (path) {
        let last;
        [path, last] = splitPath(path);
        if (last === "..") {
          path = joinPath(joinPath(path, ".."), "..");
        }
      } else {
        path = "..";
      }
      continue;
    }
    const next = joinPath(path, name);
    let isLink = false;
    try {
      isLink = fs.lstatSync(next).isSymbolicLink();
    } catch {
      isLink = false;
    }
    if (!isLink) {
      path = next;
      continue;
    }
    if (seen.has(next)) {
      const cached = seen.get(next);
      if (cached !== null) {
        path = cached;
        continue;
      }
      return [joinPath(next, rest), false];
    }
    seen.set(next, null);
    let ok;
    [path, ok] = joinRealpath(path, fs.readlinkSync(next), seen);
    if (!ok) {
      return [joinPath(path, rest), false];
    }
    seen.set(next, path);
  }
  return [path, true];
}

// LLM: 链接环按解析失败处理（与宿主在 Python 3.10–3.12 上的 RuntimeError 一致，比放行更保守）。
// 函数用途: 返回绝对路径 target 的真实目标，失败时抛错。
function realpathLoose(target) {
  const [result, ok] = joinRealpath("", target, new Map());
  if (!ok) {
    throw new Error("symlink loop");
  }
  return result || "/";
}

// 函数用途: 对应 _is_credential_filename：.env 家族与常见凭据文件名（大小写不敏感），.env.example 放行。
function isCredentialFilename(name) {
  const lowered = String(name || "").trim().toLowerCase();
  if (!lowered || lowered === ".env.example") {
    return false;
  }
  return CREDENTIAL_FILENAMES.has(lowered) || lowered === ".env" || lowered.startsWith(".env.");
}

// 函数用途: 生成与宿主 PathAccessDecision 同字段的裁决。
function decision(allowed, code = "", target = "") {
  return { allowed, code, target };
}

// LLM: 对应 PathAccessPolicy._owner_scope_decision：数据目录里只放行自己的 owner home 与 shared/。
// 函数用途: 判定 my-agent 数据目录内的 owner 级访问。
function ownerScopeDecision(policy, resolved, homeRoot) {
  if (within(resolved, policy.ownerScopeRoot) || within(resolved, joinPath(homeRoot, "shared"))) {
    return decision(true, "", resolved);
  }
  if (within(resolved, joinPath(homeRoot, "admin_grants"))) {
    return decision(false, "PATH_ADMIN_GRANTS_BLOCKED", resolved);
  }
  if (within(resolved, joinPath(homeRoot, "owners"))) {
    return decision(false, "PATH_CROSS_OWNER_BLOCKED", resolved);
  }
  return decision(false, "PATH_OWNER_SCOPE_BLOCKED", resolved);
}

// LLM: 对应 PathAccessPolicy.check：owner 墙先于 full；无 owner 墙时 normal 模式依次检查凭据文件名、
//   my-agent 数据目录豁免与危险目录。resolved 已是真实路径。
// 函数用途: 对一个已解析的目标做路径策略裁决。
function policyCheck(policy, resolved) {
  const name = resolved.slice(resolved.lastIndexOf("/") + 1);
  if (policy.ownerScopeRoot !== null) {
    if (within(resolved, policy.ownerScopeRoot)) {
      return decision(true, "", resolved);
    }
    if (policy.agentHomeRoot !== null && within(resolved, policy.agentHomeRoot)) {
      const scoped = ownerScopeDecision(policy, resolved, policy.agentHomeRoot);
      if (!scoped.allowed) {
        return scoped;
      }
      return isCredentialFilename(name) ? decision(false, "PATH_CREDENTIAL_FILE_BLOCKED", resolved) : scoped;
    }
    return decision(false, "PATH_OWNER_SCOPE_BLOCKED", resolved);
  }
  if (policy.mode === "full") {
    return decision(true, "", resolved);
  }
  if (isCredentialFilename(name)) {
    return decision(false, "PATH_CREDENTIAL_FILE_BLOCKED", resolved);
  }
  if (policy.agentHomeRoot !== null && within(resolved, policy.agentHomeRoot)) {
    return decision(true, "", resolved);
  }
  if (policy.dangerousRoots.some((root) => within(resolved, root))) {
    return decision(false, "PATH_DANGEROUS_ROOT_BLOCKED", resolved);
  }
  return decision(true, "", resolved);
}

// LLM: 对应 WorkspaceReadContext.check 与 check_with_external_roots：先解析真实目标并要求在读取根内，
//   再做路径策略；只有普通 owner 墙拒绝可以由宿主明确授权的外部根复核。不打开文件。
// 函数用途: 判断本次调用能否读取 requested；允许时 target 是应当打开的真实路径。
function check(context, requested) {
  let target;
  try {
    if (typeof requested !== "string" || requested.includes("\0")) {
      throw new Error("invalid path");
    }
    target = realpathLoose(joinPath(context.cwd, requested));
  } catch {
    return decision(false, "PATH_RESOLUTION_FAILED");
  }
  if (!context.readRoots.some((root) => within(target, root))) {
    return decision(false, "PATH_READ_SCOPE_BLOCKED", target);
  }
  const first = policyCheck(context.policy, target);
  if (first.allowed || first.code !== "PATH_OWNER_SCOPE_BLOCKED") {
    return first;
  }
  if (!context.grantedExternalRoots.some((root) => within(target, root))) {
    return first;
  }
  return policyCheck(context.externalPolicy, target);
}

module.exports = { EXTENSION, VERSION, ContextError, parseContext, check, realpathLoose };

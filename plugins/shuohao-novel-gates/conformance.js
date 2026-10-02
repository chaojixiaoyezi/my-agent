"use strict";
// LLM: 只在开发/测试时运行，不打进插件包；目录树由调用方（pytest）按用例建好，本脚本只读，不创建或修改文件。
//   输出每条用例的裁决，比较由调用方完成，便于失败时看到完整差异。
// 模块用途: 用 plugins/sdk/conformance/workspace_read_check.json 检验 src/workspace_read.js 与宿主参考实现一致。
// 用法: node conformance.js <用例 JSON> <已建好的目录树根（真实路径）>

const fs = require("fs");
const { check, parseContext } = require("./src/workspace_read.js");

// LLM: 只替换夹具占位符，不读业务路径、不修改输入树；与 Python 参考测试共享同一向量。
// 函数用途: 把用例里所有字符串中的 {root} 换成目录树根。
function substitute(value, root) {
  if (typeof value === "string") {
    return value.split("{root}").join(root);
  }
  if (Array.isArray(value)) {
    return value.map((item) => substitute(item, root));
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, substitute(item, root)]));
  }
  return value;
}

// LLM: 不进插件 ZIP，只输出逐项判定，由 pytest 与参考期望比较；不写工作区。
// 函数用途: 逐条运行有效用例与无效上下文，打印 {valid: [{allowed, code}], invalid: [是否被拒绝]}。
function main() {
  const [vectorsPath, root] = process.argv.slice(2);
  const vectors = substitute(JSON.parse(fs.readFileSync(vectorsPath, "utf8")), root);
  const contexts = Object.fromEntries(Object.entries(vectors.contexts).map(([name, payload]) => [name, parseContext(payload)]));
  const valid = vectors.valid_cases.map((item) => {
    const result = check(contexts[item.context], item.path);
    return { allowed: result.allowed, code: result.code };
  });
  const invalid = vectors.invalid_contexts.map((item) => {
    const payload = JSON.parse(JSON.stringify(vectors.contexts[item.base]));
    Object.assign(payload, item.set || {});
    if (item.drop) {
      delete payload[item.drop];
    }
    try {
      parseContext(payload);
      return false;
    } catch {
      return true;
    }
  });
  process.stdout.write(JSON.stringify({ valid, invalid }) + "\n");
}

main();

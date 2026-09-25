# LLM: 四个页面动作的业务流程，只经 PageConnection 发 CDP 命令；页面脚本参数一律用 JSON 字面量注入，不拼接用户文本到代码里。
#   每个动作都先确认当前页面地址仍被允许；导航类动作结束后再核对最终地址，跳到不允许地址时立即停到空白页并报错。
# 模块用途: 实现 open/read/click/fill 的具体步骤与结果结构。

from __future__ import annotations

import json

from .access import BLANK_URL, UrlGuard
from .cdp import PageConnection
from .errors import BrowserError

TEXT_LIMIT = 2000
SETTLE_SECONDS = 0.5

_STATE_JS = """() => ({url: location.href, title: document.title,
  text: (document.body ? document.body.innerText : "").slice(0, %d)})""" % TEXT_LIMIT

_READ_JS = """(selector) => {
  let list;
  try { list = document.querySelectorAll(selector || "input, textarea, select, button"); }
  catch (error) { return {invalid: true}; }
  const items = Array.from(list).slice(0, 50).map((el) => {
    const tag = el.tagName.toLowerCase();
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const item = {tag, text: (el.innerText || el.textContent || "").trim().slice(0, 200),
      name: el.getAttribute("name") || "", id: el.id || "",
      visible: box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none"};
    if (["input", "textarea", "select"].includes(tag)) item.value = String(el.value);
    if (["input", "button"].includes(tag)) item.type = el.type || "";
    if (tag === "select") item.options = Array.from(el.options).slice(0, 50).map((o) => ({value: o.value, text: o.text.trim()}));
    return item;
  });
  return {count: list.length, items};
}"""

_CLICK_JS = """(selector) => {
  let list;
  try { list = document.querySelectorAll(selector); } catch (error) { return {invalid: true}; }
  if (list.length !== 1) return {count: list.length};
  const el = list[0];
  el.scrollIntoView({block: "center"});
  el.click();
  return {count: 1, tag: el.tagName.toLowerCase()};
}"""

# 观察候选的第 n 个元素：按观察时的同一选择器重新查询，总数与序号都要对得上，否则返回 mismatch 让调用方报 not_found
_NTH_PREFIX = """  let list;
  try { list = document.querySelectorAll(selector); } catch (error) { return {invalid: true}; }
  if (list.length !== expected || index < 0 || index >= list.length) return {count: list.length, mismatch: true};
  const el = list[index];
"""

_CLICK_NTH_JS = "(selector, index, expected) => {\n" + _NTH_PREFIX + """  el.scrollIntoView({block: "center"});
  el.click();
  return {count: 1, tag: el.tagName.toLowerCase()};
}"""

_FILL_BODY = """  const tag = el.tagName.toLowerCase();
  const kind = (el.type || "").toLowerCase();
  const fixed = ["checkbox", "radio", "file", "submit", "button", "reset", "image", "hidden"];
  if (!["input", "textarea", "select"].includes(tag) || (tag === "input" && fixed.includes(kind)))
    return {count: 1, unfillable: tag + (kind ? "[type=" + kind + "]" : "")};
  if (tag === "select") {
    const options = Array.from(el.options);
    const match = options.find((o) => o.value === value) || options.find((o) => o.text.trim() === value);
    if (!match) return {count: 1, options: options.slice(0, 50).map((o) => o.value)};
    value = match.value;
  }
  el.focus();
  const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
  if (setter && setter.set) setter.set.call(el, value); else el.value = value;
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  return {count: 1, tag, value: String(el.value)};
}"""

_FILL_JS = """(selector, value) => {
  let list;
  try { list = document.querySelectorAll(selector); } catch (error) { return {invalid: true}; }
  if (list.length !== 1) return {count: list.length};
  const el = list[0];
""" + _FILL_BODY

_FILL_NTH_JS = "(selector, index, expected, value) => {\n" + _NTH_PREFIX + _FILL_BODY

# read 不给 selector 时的默认查询，必须与 _READ_JS 里的回退串逐字相同，观察候选才能按同一选择器解析回元素
DEFAULT_READ_SELECTOR = "input, textarea, select, button"


# LLM: 用 JSON 字面量传参（ensure_ascii 转义 U+2028 等字符），脚本异常转成 SCRIPT_FAILED，不回传页面堆栈。
# 函数用途: 在页面里执行一个函数脚本并按值取回结果。
def evaluate(page: PageConnection, function: str, *arguments: object) -> object:
    literal = ", ".join(json.dumps(item, ensure_ascii=True) for item in arguments)
    result = page.call("Runtime.evaluate", {"expression": f"({function})({literal})",
                                            "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in result:
        raise BrowserError("SCRIPT_FAILED", "页面脚本执行失败。")
    return (result.get("result") or {}).get("value")


# LLM: 有副作用：地址不允许时同步导航到空白页，避免继续停留在越界页面。about:blank 视为允许。
# 函数用途: 读取当前页面地址并要求它仍被允许，返回页面状态。
def require_current(page: PageConnection, guard: UrlGuard) -> dict:
    state = evaluate(page, _STATE_JS)
    if not isinstance(state, dict):
        raise BrowserError("SCRIPT_FAILED", "无法读取页面状态。")
    reason = guard.reason(str(state.get("url", "")))
    if reason is not None:
        # 导航被拦截时当前页是浏览器错误页，原因以被拦下的真实目标为准
        blocked = page.blocked[-1] if page.blocked else str(state.get("url", ""))
        reason = guard.reason(blocked) or reason
        page.call("Page.navigate", {"url": BLANK_URL})
        raise BrowserError("URL_NOT_ALLOWED", f"页面跳到了不允许的地址，已停止：{reason}。", url=blocked[:500])
    return state


# LLM: 导航前先裁决目标；导航被拦截（重定向到不允许地址）报 URL_NOT_ALLOWED，其它网络失败报 NAVIGATION_FAILED；
#   同文档跳转（无 loaderId）不等 load 事件。返回最终 URL、标题和可见文字前 2000 字。
# 函数用途: 打开一个受控地址并等待加载完成。
def open_page(page: PageConnection, guard: UrlGuard, value: str) -> dict:
    url = guard.target(value)
    guard.require(url)
    page.events.clear()
    page.blocked.clear()
    result = page.call("Page.navigate", {"url": url})
    if result.get("errorText"):
        if page.blocked:
            raise BrowserError("URL_NOT_ALLOWED", "页面跳到了不允许的地址，已停止。", url=page.blocked[-1])
        raise BrowserError("NAVIGATION_FAILED", f"页面打开失败：{str(result['errorText'])[:200]}")
    if result.get("loaderId") and page.wait_event(_is("Page.loadEventFired"), page.timeout) is None:
        raise BrowserError("TIMEOUT", f"页面加载超时（{page.timeout:g} 秒）。")
    return require_current(page, guard)


# LLM: 只读；不给 selector 时列表单控件与按钮。最多返回 50 个元素，count 是真实匹配总数。
# 函数用途: 读取当前页面匹配元素的标签、文字、value、name/id 和可见性。
def read_elements(page: PageConnection, guard: UrlGuard, selector: str | None) -> dict:
    state = require_current(page, guard)
    found = _checked(evaluate(page, _READ_JS, selector or ""))
    return {"url": state["url"], "title": state["title"], "count": found["count"], "items": found["items"]}


# LLM: 只点唯一匹配元素；点击后 500ms 内出现主 frame 导航就等 load（受命令超时约束），否则视为已稳定。
#   有副作用：页面状态改变。结束后核对最终地址。
# 函数用途: 点击唯一匹配元素并返回点击后的 URL 与标题。
def click_element(page: PageConnection, guard: UrlGuard, selector: str) -> dict:
    require_current(page, guard)
    page.events.clear()
    page.blocked.clear()
    return _after_click(page, guard, _unique(evaluate(page, _CLICK_JS, selector)))


# LLM: 只填唯一匹配的 input/textarea/select，并触发 input/change 事件；select 可按选项 value 或文字匹配。
# 函数用途: 填写一个表单控件并返回填后的值。
def fill_element(page: PageConnection, guard: UrlGuard, selector: str, value: str) -> dict:
    require_current(page, guard)
    return _fill_result(_unique(evaluate(page, _FILL_JS, selector, value)))


# LLM: 候选路径：按观察时的选择器与序号重新定位，总数变化或序号越界报 OBSERVATION_NOT_FOUND（结构化 my_agent_observation_error=not_found），
#   不产生副作用；导航处理与 click_element 相同。
# 函数用途: 点击观察候选对应的第 index 个元素。
def click_candidate(page: PageConnection, guard: UrlGuard, selector: str, index: int, expected: int) -> dict:
    require_current(page, guard)
    page.events.clear()
    page.blocked.clear()
    found = _resolved(evaluate(page, _CLICK_NTH_JS, selector, index, expected))
    return _after_click(page, guard, found)


# 函数用途: 填写观察候选对应的第 index 个表单控件。
def fill_candidate(page: PageConnection, guard: UrlGuard, selector: str, index: int, expected: int, value: str) -> dict:
    require_current(page, guard)
    return _fill_result(_resolved(evaluate(page, _FILL_NTH_JS, selector, index, expected, value)))


# 函数用途: 把填写脚本的返回值转成结果或结构化错误。
def _fill_result(found: dict) -> dict:
    if "unfillable" in found:
        raise BrowserError("NOT_FILLABLE", f"匹配元素是 {found['unfillable']}，只能填写 input/textarea/select。")
    if "options" in found:
        raise BrowserError("OPTION_NOT_FOUND", "下拉框里没有这个选项。", options=found["options"])
    return {"filled": found["tag"], "value": found["value"]}


# LLM: 点击后 500ms 内出现主 frame 导航就等 load（受命令超时约束），否则视为已稳定；结束后核对最终地址。
# 函数用途: 点击脚本执行后的统一收尾，返回点击结果。
def _after_click(page: PageConnection, guard: UrlGuard, found: dict) -> dict:
    started = page.wait_event(lambda event: event["method"] in ("Page.frameStartedLoading", "Page.frameRequestedNavigation")
                              and (event.get("params") or {}).get("frameId") == page.main_frame, SETTLE_SECONDS)
    navigated = started is not None
    if navigated and page.wait_event(_is("Page.loadEventFired"), page.timeout) is None:
        raise BrowserError("TIMEOUT", f"点击后页面加载超时（{page.timeout:g} 秒）。")
    state = require_current(page, guard)
    return {"clicked": found["tag"], "navigated": navigated, "url": state["url"], "title": state["title"]}


# 函数用途: 候选脚本结果校验：选择器语法错照常报，总数/序号不符报结构化 not_found。
def _resolved(found: object) -> dict:
    found = _checked(found)
    if found.get("mismatch"):
        raise BrowserError("OBSERVATION_NOT_FOUND", "观察过的元素已变化或不存在，请重新 read 后再操作。",
                           my_agent_observation_error={"code": "not_found"})
    return found


# LLM: 候选只由结构化 item 字段生成：key = 选择器哈希前 8 位 + "." + 序号（本插件在同一代次下能解析回元素，不是选择器）；
#   role 取标签/类型短标识；label 取可见文字，其次 name/id，再次“标签-序号”，去控制字符、≤120 字；actions 按控件类型给 click/fill。
# 函数用途: 把 read 的元素列表投影成宿主合同要求的观察候选。
def observation_candidates(items: list, selector_hash: str) -> list[dict]:
    candidates = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        tag, kind = str(item.get("tag") or ""), str(item.get("type") or "").lower()
        if tag == "input" and kind in ("checkbox", "radio"):
            role = kind
        elif tag == "input" and kind in ("submit", "button", "reset", "image"):
            role = "button"
        elif tag in ("input", "textarea"):
            role = "textbox"
        elif tag == "a":
            role = "link"
        else:
            role = tag or "element"
        actions = ["fill", "click"] if role in ("textbox", "select") else ["click"]
        raw = str(item.get("text") or item.get("name") or item.get("id") or f"{tag or 'element'}-{index}")
        label = "".join(ch for ch in raw if ord(ch) >= 32)[:120].strip() or f"{tag or 'element'}-{index}"
        candidates.append({"key": f"{selector_hash}.{index}", "role": role, "label": label, "actions": actions})
    return candidates


# LLM: 选择器语法错误和脚本返回非对象分别报错，其余原样返回。
# 函数用途: 校验页面脚本的查找结果。
def _checked(found: object) -> dict:
    if not isinstance(found, dict):
        raise BrowserError("SCRIPT_FAILED", "页面脚本返回值无效。")
    if found.get("invalid"):
        raise BrowserError("INVALID_SELECTOR", "CSS 选择器语法无效。")
    return found


# LLM: 0 个与多个匹配都是错误，并带上真实匹配个数。
# 函数用途: 要求选择器恰好匹配一个元素。
def _unique(found: object) -> dict:
    found = _checked(found)
    if found.get("count") == 0:
        raise BrowserError("SELECTOR_NOT_FOUND", "选择器没有匹配到元素。", count=0)
    if found.get("count") != 1:
        raise BrowserError("SELECTOR_AMBIGUOUS", f"选择器匹配到 {found.get('count')} 个元素，只能操作唯一元素。",
                           count=found.get("count"))
    return found


# LLM: 纯函数，返回按事件名匹配的谓词。
# 函数用途: 生成等待指定 CDP 事件的判断函数。
def _is(method: str):
    return lambda event: event.get("method") == method

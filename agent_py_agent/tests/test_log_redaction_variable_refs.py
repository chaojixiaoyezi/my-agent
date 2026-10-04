# LLM: 这一整改的是"打码误伤纯变量引用"：工具输出里的 $NAME / ${NAME}（环境变量写法，全大写）被换成
#   <redacted>，会让复审把正确脚本读成"请求头写死了占位符"，模型照抄还可能把字面量写进文件。
#   9b 终审必须修：变量名原来收 [A-Za-z_][A-Za-z0-9_]*，于是 "$Summer2024"、"$uper_Secret_1"、
#   "$abcDEF123xyz" 这些以 $ 开头的真密码被当变量引用放过。现在只认全大写环境变量写法，大小写混合与
#   全小写一律打码；残留边界是全大写 $ 开头字面量（如 $ADMIN123），有意接受并有用例钉住。
#   用例字面量一律用拼接构造，避免本文件自身在被写入/查看时就被打码，导致"测的是已打码的字符串"这种假绿。
# 模块用途: 钉住纯变量引用保留口径（只认全大写）、其余写法照旧打码，以及真实密钥没有任何新路径泄漏。
from __future__ import annotations

import signal
import time
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.common.log_redaction import (
    _mask_secret_value,
    _strip_wrapping_quote,
    redact_sensitive_text,
)
from agent_py_agent.agent.tooling.output_projection import redact_tool_output_text

D = "$"
MARKER = "<red" + "acted>"
GW_HEADER = "X-Gateway-" + "Token"
DELETED = "<red" + "acted-private-key>"

# 真密钥样本：按已知密钥形状构造，避免写成完整字面量被扫掉后失去测试意义。
FAKE_KEY = "sk-" + "abcdefghijklmnopqrstuvwx"


def _gw(value: str) -> str:
    return f"{GW_HEADER}: {value}"


def test_pure_variable_references_are_kept_verbatim() -> None:
    """恰好是 $NAME 或 ${NAME} 的纯变量引用不是秘密，保留原文（只认环境变量写法：全大写）。"""

    for value in (D + "GW_TOKEN", D + "{GW_TOKEN}", D + "_A1", D + "{_A1}", D + "TOKEN", D + "{TOKEN}"):
        text = _gw(value)
        for code_file in (False, True):
            safe = redact_sensitive_text(text, code_file=code_file)
            assert safe == text, (value, code_file, safe)
            assert MARKER not in safe


# LLM: 9b 终审必须修：变量名原来收 [A-Za-z_][A-Za-z0-9_]*，"以 $ 开头、后面只有字母数字下划线"的真密码
#   会被当变量引用放过。这里钉住新口径——只有全大写环境变量写法（$NAME / ${NAME}）保留，大小写混合与
#   全小写一律打码；两边代价不对等：多打一次最多让复审误会，少打一次凭据就进模型上下文和日志。
# 函数用途: 断言大小写混合、全小写、数字结尾的 $ 开头值都照旧打码。
@pytest.mark.parametrize(
    "value",
    [
        D + "Summer2024",        # YAML password: 以 $ 开头的真密码
        D + "uper_Secret_1",     # Python api_key
        D + "abcDEF123xyz",      # Authorization: Bearer 里的混合大小写值
        D + "token",             # 全小写
        D + "admin",             # 全小写
        D + "_lower",            # 下划线开头但含小写
        D + "{token}",           # 花括号写法同样只认全大写
    ],
)
def test_mixed_or_lowercase_dollar_values_are_still_redacted(value: str) -> None:
    text = _gw(value)
    safe = redact_sensitive_text(text)
    assert value not in safe
    assert MARKER in safe


# LLM: 有意接受的已知边界（3a 裁定，2026-10-04）：全大写的 $ 开头字面量（如 $ADMIN123）形状上与变量
#   引用完全一致，打码分不出"值在别处"还是"值就是这个"。这里把它钉成"当前会保留"，不是为了将来修它——
#   真要在这一档更严，会把 $GW_TOKEN 这类正确脚本一起打掉，代价方向相反。记录在案，改动须同步 DESIGN_LEDGER。
# 函数用途: 锁住"全大写 $ 开头字面量当前保留"这一已知边界，防止它被当成回归悄悄改掉。
def test_all_caps_dollar_literal_is_a_known_accepted_boundary() -> None:
    value = D + "ADMIN" + "123"
    text = _gw(value)
    safe = redact_sensitive_text(text)
    assert safe == text, "全大写 $ 开头值当前会保留：这是有意接受的边界，不是回归"
    assert MARKER not in safe


# LLM: 9b 建议 1：判定层的两条防线（正则 \Z 与 _mask_secret_value 的 fullmatch）必须同时生效——
#   "引用 + 字面量"混写（$TOKEN:abc、${TOKEN}abc、$TOKEN$OTHER）既不是纯引用，就必须打码；
#   这里直接喂 _mask_secret_value，绕开上层正则匹配，专门钉这两层。
# 函数用途: 断言带字面量尾巴或拼接的引用一定被打码，不能靠外层正则不匹配来"碰巧"安全。
@pytest.mark.parametrize(
    "value",
    [
        D + "TOKEN:abc",         # 引用后跟冒号与字面量
        D + "{TOKEN}abc",        # 花括号引用后跟字面量
        D + "TOKEN" + D + "OTHER",  # 两个引用拼在一起
        D + "TOKEN" + "abc",     # 引用后直接跟字面量
        D + "{TOKEN" + "}abc",   # 花括号形态带尾巴
    ],
)
def test_reference_plus_literal_never_passes_the_pure_reference_gate(value: str) -> None:
    assert _mask_secret_value(value, MARKER) == MARKER, value
    assert _mask_secret_value(_strip_wrapping_quote(value), MARKER) == MARKER, value


# LLM: 上面那条参数化只走 _mask_secret_value；\Z 与 fullmatch 两层都得单独钉，否则把 \Z 删掉、
#   或把 fullmatch 换成 match（前缀匹配）时，用例可能因为上游正则本来就不匹配而"碰巧"全绿。
#   这里用 _PURE_VARIABLE_REFERENCE_RE 自己的 finditer 复现两条防线：\Z 要求匹配吃完整串，
#   fullmatch 要求锚在开头；两者任一放松，下面的断言都会失败。
# 函数用途: 直接钉住正则的 \Z 与 fullmatch 两层，防止它们被悄悄放宽。
def test_regex_anchors_and_fullmatch_are_both_required() -> None:
    from agent_py_agent.agent.common.log_redaction import _PURE_VARIABLE_REFERENCE_RE

    pure = D + "TOKEN"
    # 纯引用本身必须被两种判定同时接受（否则规则根本不会生效）。
    assert _PURE_VARIABLE_REFERENCE_RE.search(pure)
    assert _PURE_VARIABLE_REFERENCE_RE.fullmatch(pure)
    assert _PURE_VARIABLE_REFERENCE_RE.search(pure).end() == len(pure), "\\Z 必须吃完整串"

    # 带尾巴/前缀的形状：两条防线里至少一条会挡住；改坏任一条都会让这里失败。
    # `search` 会在串内任意位置找匹配（可能先命中末尾那段 `$OTHER`），所以 \Z 的效果要这样核：
    # 只要整串不是纯引用，`fullmatch` 必须为 None；而 `match`（锚定开头的前缀匹配）在去掉 \Z 后
    # 会命中 `$TOKEN` 前缀——这里显式对比两者的差异，删掉 \Z 时下面第一条断言会失败。
    for value in (D + "TOKEN:abc", D + "{TOKEN}abc", D + "TOKEN" + "abc"):
        assert not _PURE_VARIABLE_REFERENCE_RE.fullmatch(value), value
        assert not _PURE_VARIABLE_REFERENCE_RE.match(value), f"\\Z 必须拒绝带尾巴的值：{value}"


# LLM: fullmatch 那一层要单独钉：把 `.fullmatch(` 换成 `.match(`（前缀匹配）时，**只有正则内部已经带 \Z
#   才会两种等价**（那时两者都要求吃到串尾）。所以真正要防的是"两层同时被放松"——只要 \Z 去掉，
#   match 与 fullmatch 立刻分叉。这条同时核 `_mask_secret_value` 的实际行为，并把"正则里必须有 \Z"
#   写成结构化断言：将来谁把 \Z 挪走、又没换成 fullmatch，这里会红。
# 函数用途: 断言 _mask_secret_value 拒收带尾巴的值，且判定依赖整串锚（\Z 或 fullmatch 至少有一层）。
def test_mask_secret_value_requires_a_whole_string_match() -> None:
    from agent_py_agent.agent.common.log_redaction import _PURE_VARIABLE_REFERENCE_RE

    assert _mask_secret_value(D + "TOKEN:abc", MARKER) == MARKER
    assert _mask_secret_value(D + "{TOKEN}abc", MARKER) == MARKER
    assert _mask_secret_value(D + "TOKENabc", MARKER) == MARKER
    assert _mask_secret_value(D + "TOKEN", MARKER) == D + "TOKEN"
    # 两层锚至少留一层：去掉 \Z 后，前缀匹配必须仍被 fullmatch 挡住（或反之）。
    tailed = D + "TOKEN" + "abc"
    assert not _PURE_VARIABLE_REFERENCE_RE.fullmatch(tailed)
    assert _PURE_VARIABLE_REFERENCE_RE.pattern.endswith("\\Z")


def test_bearer_pure_variable_reference_is_kept() -> None:
    """真实形状 curl -H "Authorization: Bearer $TOKEN" 里，值也只是纯变量引用。"""

    text = 'curl -H "' + "Authorization: Bearer " + D + 'TOKEN" http://127.0.0.1:8420/ask'
    safe = redact_sensitive_text(text)
    assert safe == text
    assert MARKER not in safe


@pytest.mark.parametrize(
    "value",
    [
        D + "{TOKEN:-literal-default}",      # 带字面量默认值
        D + "(cat /tmp/secret)",             # 命令替换
        D + "{TOKEN}suffix",                 # 拼接字面量
        D + "A" + D + "B",                   # 多个变量拼在一起
        D + "{TOKEN",                        # 未闭合
        D + "1BAD",                          # 名字不以字母/下划线开头
        D + "{1BAD}",
        D + "lowercase",                     # 全小写：按新口径不算环境变量写法
        D + "{mixedCase}",                   # 大小写混合
        "literal-value",                     # 普通字面量
        FAKE_KEY,                            # 真密钥形状
    ],
)
def test_other_shapes_are_still_redacted(value: str) -> None:
    """只要不是"整段一个纯变量引用"，一律照旧打码。"""

    text = _gw(value)
    safe = redact_sensitive_text(text)
    assert value not in safe
    assert MARKER in safe


def test_real_secret_in_bearer_header_is_still_redacted() -> None:
    """Authorization: Bearer 后面跟真值时照旧打码。"""

    text = "Authorization: Bearer " + FAKE_KEY
    safe = redact_sensitive_text(text)
    assert FAKE_KEY not in safe
    assert MARKER in safe


def test_written_key_literal_is_never_preserved_by_the_variable_rule() -> None:
    """字面量 <redacted> 本身不是变量引用，不会因为这条规则被放行。"""

    text = _gw(MARKER)
    safe = redact_sensitive_text(text)
    assert safe == text  # 值本来就已经是占位符，保持原样即可
    assert D not in safe


def test_real_marker_shifted_written_script_shape_is_kept_via_projection() -> None:
    """真实形状：ds10 复审看到的那两行脚本，经过工具输出投影后变量引用保留。"""

    token_var = D + "GW_TOKEN"
    braced = D + "{GW_TOKEN}"
    text = (
        "GW_AUTH_HEADER=(-H \"" + GW_HEADER + ": " + token_var + "\")\n"
        'curl -H "' + GW_HEADER + ": " + braced + '" http://127.0.0.1:8420/ask\n'
    )
    for redaction in ("default", "source_code"):
        safe = redact_tool_output_text(text, redaction=redaction)
        assert token_var in safe
        assert braced in safe
        assert MARKER not in safe


def test_projection_still_redacts_real_secret_in_same_shape() -> None:
    """同一形状里放真值，投影后照旧打码，没有新路径把真值漏出去。"""

    text = "GW_AUTH_HEADER=(-H \"" + GW_HEADER + ": " + FAKE_KEY + "\")"
    for redaction in ("default", "source_code"):
        safe = redact_tool_output_text(text, redaction=redaction)
        assert FAKE_KEY not in safe
        assert MARKER in safe


def test_json_form_field_still_redacts_unless_pure_variable() -> None:
    """JSON 字段形态同样只保留纯变量引用。"""

    token_key = "to" + "ken"
    json_var = '{"' + token_key + '": "' + D + "{API_TOKEN}\"}"
    json_literal = '{"' + token_key + '": "' + FAKE_KEY + '"}'
    assert D + "{API_TOKEN}" in redact_sensitive_text(json_var)
    assert FAKE_KEY not in redact_sensitive_text(json_literal)


def test_known_secret_scan_still_applies_after_variable_exemption() -> None:
    """纯变量保留后仍要过已知密钥扫描：真密钥不会被这条新规则带出去。"""

    text = "note: " + FAKE_KEY + " and " + _gw(D + "GW_TOKEN")
    safe = redact_sensitive_text(text)
    assert FAKE_KEY not in safe
    assert D + "GW_TOKEN" in safe


@pytest.mark.parametrize(
    "value",
    [
        D + 'TOKEN"x',              # 尾引号后还有字面量
        D + "TOKEN'suffix'",        # 引号后面拖着字面量
        '"' + D + "TOKEN",          # 只有开头一个引号（不是值的一部分）
    ],
)
def test_quoting_only_strips_one_wrapping_quote(value: str) -> None:
    """剥离引号只允许"一层外层引号"：值里再拖字面量时必须照旧打码。

    这条钉住 M5 变异（把所有引号删掉再判定）——它会把 __TOKEN"x 这类值误判成纯引用。
    只断言到辅助函数层：这些形状在 _SECRET_ASSIGNMENT_RE 那里本来就不匹配，整串不会被打码，
    基线（d05a0d075）行为相同，不能拿整串断言当这条规则的效果。
    """

    assert _mask_secret_value(value, MARKER) == MARKER
    assert _mask_secret_value(D + "GW_TOKEN", MARKER) == D + "GW_TOKEN"


def test_one_wrapping_quote_is_still_enough_for_a_pure_reference() -> None:
    """只脱一层引号：`$TOKEN"` 这种（正则把结尾引号带进来）仍算纯引用。"""

    assert _mask_secret_value(D + 'TOKEN"', MARKER) == D + 'TOKEN"'
    assert _mask_secret_value('"' + D + 'TOKEN"', MARKER) == '"' + D + 'TOKEN"'
# LLM: rkey——键名按"完整末尾片段"认，不是子串，也不是原来那个断不开下划线的 \b。基线里
#   DB_PASSWORD=hunter22、api_token=abcd1234、MY_SECRET: xyz12345 因为 \b 撞上下划线而根本没打码，
#   凭据就原样进了日志和模型上下文；这里钉住这三类带前缀的键名必须打码。
# 函数用途: 断言带下划线/连字符前缀的凭据键名会被打码。
@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD=" + "hunter22",         # 下划线前缀 + 大写下划线键名
        "api_token=" + "abcd1234",           # 小写下划线前缀
        "MY_SECRET: " + "xyz12345",          # 冒号分隔、无引号
        "my-secret=" + "abcd1234",           # 连字符前缀（HTTP 头/命令行风格）
        "AWS_SECRET_ACCESS_KEY=" + "abcd1234",
    ],
)
def test_prefixed_credential_key_names_are_redacted(text: str) -> None:
    safe = redact_sensitive_text(text)
    assert MARKER in safe, text
    assert "hunter22" not in safe and "abcd1234" not in safe and "xyz12345" not in safe, text


# LLM: 同一条改动的反面：按末尾完整片段认，就不能把只在中间出现凭据词的普通参数误打码——
#   max_tokens 的 token 是复数（不以 token 结尾）、token_count 的 token 在开头、password_hint 的
#   password 也不在结尾。这些被误打码会让 /settings 回显和记账看不清正常数值。
# 函数用途: 断言普通参数名不被末尾片段规则误伤。
@pytest.mark.parametrize(
    "text",
    ["max_tokens=4096", "token_count=12", "password_hint=x1234", "tokens=4096",
     "secretary=abcd1234", "my_password_hint=x1234"],
)
def test_ordinary_keys_are_not_mistaken_for_credentials(text: str) -> None:
    safe = redact_sensitive_text(text)
    assert MARKER not in safe, text
    assert safe == text


# LLM: 源码模式本来就在调用处整段跳过赋值规则（见 redact_sensitive_text 的 `if not code_file`），
#   这次改键名匹配不该影响它：同样的 DB_PASSWORD=... 在 code_file=True 下照旧不打码（源码示例不能被毁）。
# 函数用途: 断言源码模式下赋值规则仍被跳过，键名匹配的改动不改变这一行为。
def test_source_mode_still_skips_the_assignment_rule() -> None:
    text = "DB_PASSWORD=" + "hunter22"
    assert MARKER not in redact_sensitive_text(text, code_file=True)
    assert MARKER in redact_sensitive_text(text, code_file=False)


# LLM: 新键名规则不能顺手掐掉纯变量引用豁免：DB_PASSWORD=$DB_PASSWORD 整段仍是纯引用（全大写），
#   要保留原文，否则复审又会把"取了同名环境变量"读成"写死了占位符"。
# 函数用途: 断言带前缀键名 + 全大写纯引用仍然保留。
def test_prefixed_key_with_pure_reference_is_kept() -> None:
    text = "DB_PASSWORD=" + D + "DB_PASSWORD"
    assert redact_sensitive_text(text) == text
    assert MARKER not in redact_sensitive_text(text)


# LLM: rkey 的键名前缀曾经写成 (?:[A-Za-z0-9_.]*[_-])*——嵌套量词，下划线既能被内层也能被外层吃，
#   匹配失败时回溯指数增长（9b 实测 21 个下划线 0.15 秒、29 个 37.7 秒）。打码函数要处理每一段工具
#   输出、每一行日志，Markdown 分隔线、ASCII 表格、日志横线这类长串下划线就能卡住回合和 Gateway 线程。
#   这里用"上万字符的同类输入"把线性复杂度钉住：超时阈值取得很宽（0.1 秒，实测都在毫秒级），
#   目的是抓"又变成指数级"这种量级差异，不是抓 CI 抖动。计时用单调时钟。
# 函数用途: 用长下划线/连字符/蛇形输入验证打码耗时是线性的，不因输入变长而爆炸。
_PERF_BUDGET_SECONDS = 0.1
_PERF_INPUTS = (
    ("下划线长行", "_" * 20000 + "!"),
    ("a_ 重复", "a_" * 15000 + "!"),
    ("长 snake_case 键名（无 =）", "x" + "_part" * 10000),
    ("连字符长行", "-" * 20000),
)


@pytest.mark.parametrize(("label", "text"), _PERF_INPUTS)
def test_long_underscore_inputs_stay_linear(label: str, text: str) -> None:
    _assert_redacts_within_budget(label, text)


@contextmanager
def _hard_time_limit(seconds: int):
    """给单个用例套一个墙钟兜底。

    LLM: 性能用例的断点是 0.1 秒，但如果改动真把复杂度变回指数级，光跑完这次调用就要几十分钟到小时级，
       计时断言永远等不到，整个测试进程会挂死。这里用 SIGALRM 在同一个进程里硬中断，抛出超时错误让
       用例直接变红——是"抓变异"的兜底，不是常规路径。平台不支持 SIGALRM（非 Unix）时降级为不设限，
       计时断言仍单独成立。
    """
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _on_timeout(signum, frame):  # noqa: ANN001 - 信号回调签名固定
        raise TimeoutError(f"打码超过 {seconds} 秒仍未返回（复杂度退化了吗？）")

    previous = signal.signal(signal.SIGALRM, _on_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


# LLM: 统一的"打码必须在线性时间内返回"断言：先套墙钟兜底（防变异跑成小时级），再用单调时钟量实际耗时。
#   阈值 0.1 秒相对实测的毫秒级留了上百倍余量，只抓量级差异（比如回退成嵌套量词），不抓 CI 抖动。
# 函数用途: 断言一段输入的打码耗时在预算内，超时或超预算都让用例变红。
def _assert_redacts_within_budget(label: str, text: str, budget: float = _PERF_BUDGET_SECONDS) -> float:
    with _hard_time_limit(10):
        started = time.perf_counter()
        redact_sensitive_text(text)
        elapsed = time.perf_counter() - started
    assert elapsed < budget, f"{label} 打码耗时 {elapsed:.3f}s，超过 {budget}s（复杂度退化了吗？）"
    return elapsed


# LLM: rkey2 修 ReDoS 时把键名判定搬进回调，词表同一份字符串既拼正则又做集合判定，
#   于是 `api_?key` 这种带正则符号的写法会让集合里根本没有 `api_key`（`api_?key` != `api_key`），
#   功能悄悄变松——`api_key = ...` 不再被打码。这条钉住词表是纯字面量，并逐词验证判定可用。
# 函数用途: 断言凭据词表不含正则元字符，且每个词都能被 _is_credential_key_name 认出来。
def test_credential_word_list_is_literal_and_matchable() -> None:
    from agent_py_agent.agent.common import log_redaction as redaction

    words = redaction._CREDENTIAL_KEY_NAME_SET
    assert words, "词表不能为空"
    for word in words:
        assert not any(character in word for character in "?*+[](){}|\\"), f"词表混进正则元字符: {word}"
        assert redaction._is_credential_key_name(word)
        assert redaction._is_credential_key_name("DB_" + word.upper())
    # 全小写归一：settings 那份按原样比，这里必须大小写都认。
    for name in ("api_key", "API_KEY", "Api_Key", "x-api-key"):
        assert redaction._is_credential_key_name(name), name
    for name in ("max_tokens", "token_count", "password_hint", "secretary", "tokens"):
        assert not redaction._is_credential_key_name(name), name


# LLM: rkey3：JSON 字段规则原来按精确字段名枚举，`{"db_password": ...}`、`{"api-token": ...}`、
#   `{"AWS_SECRET_ACCESS_KEY": ...}` 这类带前缀的 JSON 键一律不打码，而文本赋值规则（rkey/rkey2）
#   已经按"完整末尾片段"认了同一批键名——同一个值写在 YAML 里会被打码、包成 JSON 就漏出去。
#   现在两处共用同一个 _is_credential_key_name：正则只取整段带引号键名（单量词、不嵌套），
#   判定搬到回调里；不是凭据键就整段原样返回。
# 函数用途: 断言带前缀的 JSON 键名会被打码，形状与文本赋值规则一致。
@pytest.mark.parametrize(
    "key",
    [
        "db_password",           # 下划线前缀
        "api-token",             # 连字符前缀
        "AWS_SECRET_ACCESS_KEY", # 大写下划线多段前缀
        "password",              # 正好是凭据词
    ],
)
def test_json_prefixed_credential_keys_are_redacted(key: str) -> None:
    text = '{"' + key + '": "' + "hunter22" + '"}'
    safe = redact_sensitive_text(text)
    assert "hunter22" not in safe, (key, safe)
    assert MARKER in safe


# LLM: 与上一条配对的反例：键名里出现凭据词但并不是凭据键（前缀/后缀把它变成另一个词），
#   JSON 规则不能跟着放宽成子串匹配，否则 max_tokens、token_count、password_hint 这类普通字段
#   会被打码，配置和模型输出被无辜涂掉。
# 函数用途: 断言含凭据词的普通 JSON 键名不会被误伤。
@pytest.mark.parametrize("key", ["max_tokens", "token_count", "password_hint", "note"])
def test_json_non_credential_keys_are_kept(key: str) -> None:
    text = '{"' + key + '": "keep-me-please"}'
    safe = redact_sensitive_text(text)
    assert safe == text, (key, safe)
    assert MARKER not in safe


# LLM: 键名判定搬进回调后仍要保留 rdv2 的纯变量豁免：值是整段全大写变量引用时保留原文，
#   否则复审又会把 `{"db_password": "$DB_PASSWORD"}` 读成写死的占位符。
# 函数用途: 断言 JSON 形态下纯变量引用值（含花括号写法）保留原文。
def test_json_value_that_is_a_pure_variable_reference_is_kept() -> None:
    for value in (D + "DB_PASSWORD", D + "{DB_PASSWORD}"):
        text = '{"db_password": "' + value + '"}'
        assert redact_sensitive_text(text) == text, text
        assert MARKER not in redact_sensitive_text(text)


# LLM: 源码模式在调用处整段跳过 JSON 与赋值规则（见 redact_sensitive_text 的 `if not code_file`）。
#   rkey3 把 JSON 规则换成回调写法，不能顺手把这道跳过也改掉——源码里的示例一旦被涂掉就没法读。
# 函数用途: 断言源码模式仍然跳过 JSON 字段规则，默认模式照旧打码。
def test_source_mode_still_skips_the_json_field_rule() -> None:
    text = '{"db_password": "' + "hunter22" + '"}'
    assert MARKER not in redact_sensitive_text(text, code_file=True)
    assert MARKER in redact_sensitive_text(text, code_file=False)


# LLM: 9b 建议、3a 采纳：键名去掉 128 字符上限。加上限时"200 字符前缀 + -password="这类凭据键
#   匹配不到，值原样漏出，相对 rdv2 是回归；上限对性能也没必要——键名是单个量词、前面又要求
#   非键名字符，回溯是线性的。这里把两处规则（文本赋值 + JSON 字段）的超长键名都钉住。
# 函数用途: 断言超长前缀的凭据键仍被打码，且耗时在线性范围内。
def test_over_long_credential_key_names_are_still_redacted() -> None:
    long_prefix = "a" * 20000
    text = long_prefix + "_password=" + "Sup3rSecret"
    _assert_redacts_within_budget("超长文本键名", text)
    assert "Sup3rSecret" not in redact_sensitive_text(text)

    json_text = '{"' + long_prefix + '-password": "' + "Sup3rSecret" + '"}'
    _assert_redacts_within_budget("超长 JSON 键名", json_text)
    assert "Sup3rSecret" not in redact_sensitive_text(json_text)


# LLM: 长 JSON 是打码函数在真实日志里会遇到的形状（一行里塞着超长键名和超长值）。JSON 规则换了写法，
#   这里确认它没有引入回溯：超长键名、超长值各自上万字符，都必须在预算内返回。
# 函数用途: 断言一行超长 JSON 的打码耗时在线性范围内。
def test_long_json_line_redacts_within_budget() -> None:
    long_key = "k" * 20000
    long_value = "v" * 20000
    _assert_redacts_within_budget("超长 JSON 行", '{"' + long_key + '": "' + long_value + '"}')
    nested_quotes = '{"a": "' + '"x' * 10000 + '"}'
    _assert_redacts_within_budget("多引号 JSON 行", nested_quotes)

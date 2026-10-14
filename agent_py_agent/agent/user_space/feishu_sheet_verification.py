# LLM: 只在成功的原表格写入后读回；同 adapter/后端/资源身份，不猜追加行，不重放写入，核验失败不能改成 not_started。
# 模块用途: 将表格写入已接受与读回一致性拆成结构化事实，避免把供应商回执当成内容验证。
from __future__ import annotations

from urllib.parse import quote

from ..adapter.feishu_cloud import FeishuCloudError
from .feishu_sheet_authorization import _A1_RECTANGLE, _column_index


# LLM: 返回事实不含 values 或异常正文；开关只关闭额外 GET，provider 已接受事实不随读回失败撤销。
# 函数用途: 对字面单元格执行一次原身份读回，给出 verified/mismatch/unverifiable/disabled，不修改或重试业务数据。
def verify_sheet_write(adapter, request, params: dict, result: dict, *, enabled: bool) -> dict:
    facts = {'write_accepted': True, 'status': 'unverifiable', 'reason': 'write_range_unknown'}
    if not enabled:
        return {**facts, 'status': 'disabled', 'reason': 'verification_disabled'}
    read_range = _verification_range(params, result)
    if not read_range:
        return facts
    facts['range'] = read_range
    if any(type(value) not in {str, int, float, bool} or isinstance(value, str) and value.startswith('=')
           for row in params['values'] for value in row):
        return {**facts, 'reason': 'nonliteral_values'}
    path = '/sheets/v2/spreadsheets/' + quote(params['resource_token'], safe='') + '/values/' + quote(read_range, safe='')
    try:
        data = request(adapter, 'GET', path, None)
    except FeishuCloudError as exc:
        return {**facts, 'reason': 'readback_failed', 'read_error': {'error_code': exc.code, **exc.public_facts()}}
    except Exception:  # noqa: BLE001 - 读回展示失败不能撤销已接受写入
        return {**facts, 'reason': 'readback_failed'}
    block = data.get('valueRange') if isinstance(data, dict) else None
    if not isinstance(block, dict) or block.get('range') != read_range:
        return {**facts, 'reason': 'readback_range_unconfirmed'}
    values = block.get('values')
    if not isinstance(values, list) or len(values) != len(params['values']) or any(not isinstance(row, list) for row in values):
        return {**facts, 'reason': 'readback_shape_unknown'}
    return {**facts, **_compare_cells(params['values'], values)}


# LLM: 追加只能用官方 updates.updatedRange，更新读取输入真实载荷矩形；目标 sheet 必须同 namespace，不接受 provider 跨表或开放范围。
# 函数用途: 确定一次读回的准确 A1 范围，不依据用户文字、表格现有内容或预计空行猜位置。
def _verification_range(params: dict, result: dict) -> str:
    parts = params['range'].split('!')
    if len(parts) != 2 or not parts[0]:
        return ''
    sheet_id, cell_range = parts
    if params['action'] == 'sheet_append':
        updates = result.get('updates') if isinstance(result, dict) else None
        declared = updates.get('updatedRange') if isinstance(updates, dict) else None
        if not isinstance(declared, str):
            return ''
        actual = declared.split('!')
        if len(actual) != 2 or actual[0] != sheet_id:
            return ''
        cell_range = actual[1]
    match = _A1_RECTANGLE.fullmatch(cell_range)
    if not match:
        return ''
    col, row, end_col, end_row = match.groups()
    width, height = max(map(len, params['values'])), len(params['values'])
    if params['action'] == 'sheet_update' and 'updatedRange' in result:
        reported = result['updatedRange']
        if not isinstance(reported, str) or not reported.startswith(sheet_id + '!'):
            return ''
        actual = _A1_RECTANGLE.fullmatch(reported[len(sheet_id) + 1:])
        if not actual:
            return ''
        c1, r1, c2, r2 = actual.groups()
        if (int(r1) != int(row) or _column_index(c1) != _column_index(col)
                or int(r2 or r1) < int(row) + height - 1
                or _column_index(c2 or c1) < _column_index(col) + width - 1):
            return ''
    if params['action'] == 'sheet_append' and (
            int(end_row or row) - int(row) + 1 != height
            or _column_index(end_col or col) - _column_index(col) + 1 != width):
        return ''
    end = _column_name(_column_index(col) + width - 1) + str(int(row) + height - 1)
    return sheet_id + '!' + col.upper() + row + ':' + end


# LLM: A1 逆转换只来自已验证正列序号；与保护几何共用同一列索引语义。
# 函数用途: 给读回的真实载荷矩形生成末列名，不发请求或改状态。
def _column_name(index: int) -> str:
    name = ''
    while index:
        index, value = divmod(index - 1, 26)
        name = chr(ord('A') + value) + name
    return name


# LLM: 只比较原字面值；不同表示类型是 unverifiable，不能把公式/格式转换当错写；同类型不一致是 mismatch，不触发重写。
# 函数用途: 核对每个传入单元格，结果只公开状态，不返回输入或读回的私密正文。
def _compare_cells(expected: list[list], actual: list[list]) -> dict:
    changed = False
    width = max(map(len, expected))
    for sent, read in zip(expected, actual):
        if len(read) < len(sent) or len(read) > width:
            return {'status': 'unverifiable', 'reason': 'readback_shape_unknown'}
        for before, after in zip(sent, read):
            numeric = type(before) in {int, float} and type(after) in {int, float}
            if type(before) is not type(after) and not numeric:
                return {'status': 'unverifiable', 'reason': 'representation_transformed'}
            changed = changed or before != after
    return {'status': 'mismatch' if changed else 'verified', 'reason': 'values_differ' if changed else 'literal_values_match'}

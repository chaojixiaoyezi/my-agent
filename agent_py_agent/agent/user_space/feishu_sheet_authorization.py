# LLM: 表格保护是云资源编辑许可之下的独立边界；每次现查官方结构字段，不能借应用的编辑权、lockInfo 文案或群成员私人编辑权。
# 模块用途: 在宿主代写前核对目标工作表保护范围，正规 A1 更新按相交范围判断，追加按实际目的行未知的边界保守核验。
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from ..adapter.feishu_cloud import FeishuCloudError, request_cloud_api
from .feishu_cloud_authorization import CloudResource

_A1_RECTANGLE = re.compile(r'\$?([A-Za-z]+)\$?([1-9][0-9]*)(?::\$?([A-Za-z]+)\$?([1-9][0-9]*))?')


# LLM: 管理员准确资源/操作许可保持已有显式权威；自助许可必须完成保护事实核验才发写请求，不将应用权限扩展为用户权限。
# 函数用途: 核对更新相交或追加可能触及的所有保护范围，失败时不执行业务写入。
def authorize_sheet_write(adapter, resource: CloudResource, params: dict, authorization: dict) -> None:
    if params['action'] not in {'sheet_append', 'sheet_update'} or authorization['source'] == 'administrator_grant':
        return
    parts = params['range'].split('!')
    if len(parts) != 2 or not parts[0]:
        raise _unknown('无法从 range 核验准确工作表编号。')
    sheet_id, cell_range = parts
    protections = _sheet_protections(adapter, resource.resource_token, sheet_id)
    if not protections:
        return
    rectangle = _update_rectangle(cell_range, params['values']) if params['action'] == 'sheet_update' else None
    for protection in _protection_details(adapter, resource.resource_token, sheet_id, protections):
        if rectangle is not None and not _intersects(protection.get('dimension'), sheet_id, rectangle):
            continue
        if authorization.get('principal_type') != 'openid':
            raise _denied('当前群不能借用个人的保护范围编辑权；请调整群共享范围或申请准确的管理员操作授权。')
        if not _is_editor(protection, authorization.get('principal_id', '')):
            raise _denied('当前本人没有目标保护范围编辑权；请在飞书保护设置中授权本人或改用未受保护的范围。')


# LLM: extFields 必须显式申请保护事实；只有唯一准确 sheet 与显式保护数组能证明无保护，缺字段和非表格块不能当作开放权限。
# 函数用途: 读取当前目标工作表的所有保护编号，不返回其它工作表信息。
def _sheet_protections(adapter, token: str, sheet_id: str) -> list[dict]:
    path = '/sheets/v2/spreadsheets/' + quote(token, safe='') + '/metainfo?'
    data = request_cloud_api(adapter, 'GET', path + urlencode({'extFields': 'protectedRange', 'user_id_type': 'open_id'}))
    sheets = data.get('sheets')
    if not isinstance(sheets, list) or any(not isinstance(row, dict) for row in sheets):
        raise _unknown('飞书未返回可核验的工作表保护信息。')
    matched = [row for row in sheets if row.get('sheetId') == sheet_id]
    if len(matched) != 1 or 'blockInfo' in matched[0]:
        raise _unknown('目标工作表归属无法核验，或该对象不是可写表格。')
    protections = matched[0].get('protectedRange')
    if not isinstance(protections, list) or any(not isinstance(row, dict) for row in protections):
        raise _unknown('目标工作表保护字段缺失，未使用应用权限代写。')
    ids = [row.get('protectId') for row in protections]
    if any(not isinstance(value, str) or not value or ',' in value for value in ids) or len(set(ids)) != len(ids):
        raise _unknown('飞书返回了无效或重复的保护范围编号。')
    if any(row.get('sheetId') != sheet_id for row in protections):
        raise _unknown('保护范围的工作表归属不一致。')
    return protections


# LLM: 官方接口每批最多五个准确 protectId；响应必须一一对应当前工作表，不允许缺失范围或重复范围变成漏检。
# 函数用途: 获取保护维度和准确 openId 编辑者列表，所有请求均为只读。
def _protection_details(adapter, token: str, sheet_id: str, protections: list[dict]) -> list[dict]:
    ids = [row['protectId'] for row in protections]
    path = '/sheets/v2/spreadsheets/' + quote(token, safe='') + '/protected_range_batch_get?'
    results = []
    for index in range(0, len(ids), 5):
        expected = ids[index:index + 5]
        data = request_cloud_api(adapter, 'GET', path + urlencode({'protectIds': ','.join(expected), 'memberType': 'openId'}))
        rows = data.get('protectedRanges')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise _unknown('飞书未返回可核验的保护范围详情。')
        actual = [row.get('protectId') for row in rows]
        if len(actual) != len(expected) or any(not isinstance(value, str) for value in actual) or set(actual) != set(expected):
            raise _unknown('保护范围详情与当前工作表清单不一致。')
        if any(row.get('sheetId') != sheet_id for row in rows):
            raise _unknown('保护范围详情归属无法核验。')
        results.extend(rows)
    return results


# LLM: 正规 A1 与二维 values 共同定义保守矩形；开放或未知格式在存在保护时不猜写入边界，values 超出声明范围也纳入核验。
# 函数用途: 将准确更新范围转换为从一开始的闭区间行列坐标，与飞书保护字段口径一致。
def _update_rectangle(cell_range: str, values: list[list]) -> tuple[int, int, int, int]:
    match = _A1_RECTANGLE.fullmatch(cell_range)
    if not match:
        raise _unknown('存在保护范围时，更新需使用完整 A1 矩形范围；无法核验开放或未知范围格式。')
    col1, row1, col2, row2 = match.groups()
    row_start, col_start = int(row1), _column_index(col1)
    row_end, col_end = int(row2 or row1), _column_index(col2 or col1)
    if row_end < row_start or col_end < col_start:
        raise _unknown('更新范围的起止顺序无法核验。')
    return (row_start, max(row_end, row_start + len(values) - 1),
            col_start, max(col_end, col_start + max(map(len, values)) - 1))


# LLM: 列转换只用于正规 A1 几何，不做自然语言、标题或内容推断。
# 函数用途: 将 A、Z、AA 这样的列地址转换为从一开始的列序号。
def _column_index(name: str) -> int:
    value = 0
    for char in name.upper():
        value = value * 26 + ord(char) - ord('A') + 1
    return value


# LLM: 官方保护起止索引从一开始且两端包含；空 dimension 是整工作表保护，损坏或陌生维度必须失败关闭。
# 函数用途: 判断本次准确更新矩形是否触及受保护的行或列。
def _intersects(dimension, sheet_id: str, rectangle: tuple[int, int, int, int]) -> bool:
    if dimension is None or dimension == {}:
        return True
    if not isinstance(dimension, dict) or dimension.get('sheetId') != sheet_id:
        raise _unknown('保护范围维度归属无法核验。')
    start, end, major = dimension.get('startIndex'), dimension.get('endIndex'), dimension.get('majorDimension')
    if type(start) is not int or type(end) is not int or start < 1 or end < start or major not in {'ROWS', 'COLUMNS'}:
        raise _unknown('保护范围维度或起止索引无法核验。')
    low, high = rectangle[:2] if major == 'ROWS' else rectangle[2:]
    return low <= end and high >= start


# LLM: 只接受响应中准确 openId 编辑者身份；lockInfo 只是展示文案，群不能借任一成员、应用或所有者私人权限。
# 函数用途: 核对本用户是否被明确列为这个保护范围的编辑者。
def _is_editor(protection: dict, member_id: str) -> bool:
    editors = protection.get('editors')
    users = editors.get('users') if isinstance(editors, dict) else None
    if not isinstance(users, list) or any(not isinstance(row, dict) for row in users):
        raise _unknown('保护范围的编辑者列表无法核验。')
    return bool(member_id) and any(row.get('memberType') == 'openId' and row.get('memberId') == member_id for row in users)


# LLM: 固定宿主错误保留未知事实边界，不把不完整 API 响应转成无保护的许可。
# 函数用途: 返回保护事实无法确认的可读原因。
def _unknown(message: str) -> FeishuCloudError:
    return FeishuCloudError('CLOUD_RESOURCE_PERMISSION_UNKNOWN', message)


# LLM: 明确没有保护范围权限属于授权拒绝，不表示请求已发送或副作用未知。
# 函数用途: 返回本人或群保护权限不足的可读原因。
def _denied(message: str) -> FeishuCloudError:
    return FeishuCloudError('CLOUD_RESOURCE_RANGE_NOT_AUTHORIZED', message)

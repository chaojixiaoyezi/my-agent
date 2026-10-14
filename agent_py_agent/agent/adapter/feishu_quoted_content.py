# LLM: 这里只做引用内容的纯投影；消息、群、话题和下载权限证明由 quoted_media 原入口负责，正文不能授予能力。
#   显示树之外的回调、身份、配置和凭据不递归；未知协议只说明可见覆盖，不伪称完整读取或添加字符截断权威。
# 模块用途: 从已核验的单条飞书消息中提取文字、链接、代码和显式媒体引用，明确不可读与部分覆盖。
from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Literal

from .feishu_media import extract_feishu_content, post_content_block

MAX_VISIBLE_NODES = 4096
MAX_VISIBLE_DEPTH = 32
_KNOWN_TYPES = frozenset({'text', 'post', 'image', 'file', 'audio', 'media'})
_DISPLAY_FIELDS = frozenset({
    'card', 'header', 'body', 'elements', 'content', 'text', 'title', 'subtitle', 'label',
    'href', 'url', 'description', 'alt', 'fields', 'columns', 'rows', 'cells', 'children', 'note', 'img_list',
})
_LOCALE_FIELDS = frozenset({'i18n_elements', 'i18n_header'})
_HIDDEN_FIELDS = frozenset({
    'sender', 'config', 'value', 'behaviors', 'actions', 'params', 'credentials', 'metadata',
})
_POST_FIELDS = ('tag', 'text', 'content', 'language', 'href', 'image_key', 'file_key', 'file_name')
_POST_TAGS = frozenset({'text', 'md', 'a', 'code_block', 'img', 'file', 'hr'})


# LLM: 冻结的是字符串和媒体键元组；media 每次返回原 items 形状的新副本，调用方不能篡改解析结果。
# 类用途: 保存只读的引用文字、资源和覆盖事实；不包含供应商原 JSON、身份或下载路径。
@dataclass(frozen=True)
class QuotedContentProjection:
    text: str
    coverage: Literal['complete', 'visible_only', 'unavailable']
    unreadable_count: int
    _items: tuple[tuple[tuple[str, str], ...], ...] = field(default=(), repr=False)

    # LLM: 只恢复原媒体 dict/items 数据形状，不执行请求、不授予资源来源能力；修改副本不影响冻结值。
    # 函数用途: 给原下载准备流程提供可独立修改的媒体引用副本。
    @property
    def media(self) -> dict[str, Any]:
        return {'items': [dict(item) for item in self._items]} if self._items else {}


# LLM: 单次解析预算只约束显示节点遍历，不能当作正文长度或消息权限；不读取隐藏字段、不写任何状态。
# 类用途: 在纯解析期间累计可见材料与缺口，限制嵌套深度、节点数和循环输入。
@dataclass
class _VisibleScan:
    parts: list[str] = field(default_factory=list)
    items: list[dict[str, str]] = field(default_factory=list)
    seen_media: set[tuple[str, str]] = field(default_factory=set)
    seen_nodes: set[int] = field(default_factory=set)
    visited: int = 0
    unreadable: int = 0
    partial: bool = False

    # LLM: 缺口是客观解析覆盖，不从正文关键词猜类型、授权或静默；计数是发现的缺口，不猜未访问子树规模。
    # 函数用途: 标记一个尚未读到的显示片段或安全预算停止点。
    def missing(self) -> None:
        self.partial = True
        self.unreadable += 1

    # LLM: 字符串是可见资料，不扫描 token 等词；只拒绝没有实质可见字符的空材料。
    # 函数用途: 原样保留文字、代码或链接，不发请求、不执行内容。
    def text(self, value: object) -> None:
        if isinstance(value, str) and value.strip():
            self.parts.append(value)

    # LLM: 只认当前显示节点的显式资源键，img_key 归一为 image_key；键不表示下载已授权或成功。
    # 函数用途: 去重并保存图片和开放格式文件引用，沿用原 items 模式。
    def media(self, node: dict[str, Any]) -> None:
        for key, names in (('image_key', ('image_key', 'img_key')), ('file_key', ('file_key',))):
            value = next((node[name] for name in names if isinstance(node.get(name), str) and node[name].strip()), '')
            if not value or (key, value) in self.seen_media:
                continue
            self.seen_media.add((key, value))
            item = {key: value}
            if key == 'file_key' and isinstance(node.get('file_name'), str):
                item['file_name'] = node['file_name']
            self.items.append(item)
        tag = node.get('tag') if isinstance(node.get('tag'), str) else ''
        required = ('image_key', 'img_key') if tag in {'img', 'image'} else ('file_key',) if tag in {'file', 'audio', 'media', 'video'} else ()
        if required and not any(isinstance(node.get(key), str) and node[key].strip() for key in required):
            self.missing()

    # LLM: 深度/节点/循环检查在显示节点展开前执行；预算终止明确降级覆盖，不能偷偷漏掉后续材料。
    # 函数用途: 为显示树遍历和原 post 节点解析共享一个有界计数入口。
    def admit(self, node: object, depth: int) -> bool:
        if depth > MAX_VISIBLE_DEPTH or self.visited >= MAX_VISIBLE_NODES:
            self.missing()
            return False
        self.visited += 1
        if isinstance(node, (dict, list)):
            if id(node) in self.seen_nodes:
                self.missing()
                return False
            self.seen_nodes.add(id(node))
        return True

    # LLM: 仅白名单显示边可进入递归；嵌套消息没有自己的来源证明，不能把合并转发的子正文当当前父消息。
    # 函数用途: 遍历卡片和未知消息的可见树，保留资源与直接可读内容。
    def walk(self, node: object, depth: int = 0, *, locales: bool = False) -> None:
        if not self.admit(node, depth):
            return
        if isinstance(node, str):
            self.text(node)
        elif isinstance(node, list):
            for child in node:
                if self.visited >= MAX_VISIBLE_NODES:
                    self.missing()
                    break
                self.walk(child, depth + 1)
        elif isinstance(node, dict):
            self.walk_mapping(node, depth, locales=locales)
        elif node is not None:
            self.missing()

    # LLM: 隐藏字段永不递归；根/嵌套消息信封都缺自己的来源证明，不能借正文中的 message/upper ID 恢复。
    # 函数用途: 仅从显示对象读取正文、链接和资源，排除当前内容中再次嵌入的原消息信封。
    def walk_mapping(self, node: dict[str, Any], depth: int, *, locales: bool) -> None:
        if _message_envelope(node):
            self.missing()
            return
        self.media(node)
        for key, value in islice(node.items(), MAX_VISIBLE_NODES):
            if key in _HIDDEN_FIELDS:
                if key == 'actions' and value:
                    self.missing()
                continue
            if key in _DISPLAY_FIELDS or (locales and isinstance(value, (list, dict))):
                if self.visited >= MAX_VISIBLE_NODES:
                    self.missing()
                    break
                self.walk(value, depth + 1)
            elif key in _LOCALE_FIELDS:
                self.walk(value, depth + 1, locales=True)
        if len(node) > MAX_VISIBLE_NODES:
            self.missing()


# LLM: 所有显示解析路径共用这个纯结构谓词；必须同时有消息标识和 body/content 信封字段，不看正文或孤立 ID。
# 函数用途: 区分原消息信封与普通显示节点，避免借父消息证明读取另一个尚未核验的消息。
def _message_envelope(node: dict[str, Any]) -> bool:
    message_fields = ('message_id', 'msg_type', 'message_type', 'upper_message_id')
    return any(key in node for key in message_fields) and ('body' in node or 'content' in node)


# LLM: JSON 仅解码成数据，不 dump 整体正文；JSON 形状的坏数据和过深 JSON 不回落成原始供应商 body。
# 函数用途: 接收原接口的对象或 JSON 字符串；数字/null 等直接文本不因可被解码成标量而丢失。
def _decode(raw: object) -> object:
    if not isinstance(raw, str):
        return raw if isinstance(raw, (dict, list)) else None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, (dict, list, str)) else raw
    except (ValueError, RecursionError):
        return None if raw.lstrip().startswith(('{', '[', '"')) else raw


# LLM: 原 post locale 选择只在显示容器执行；根、post 包装及选中的 locale 不能是另一个未核验消息信封。
# 函数用途: 选择一份可读 post 包，沿同一结构守门复用原语言选择和格式化。
def _post_root(content: dict[str, Any]) -> dict[str, Any]:
    root = content.get('post') if isinstance(content.get('post'), dict) else content
    if _message_envelope(content) or _message_envelope(root):
        return {}
    if isinstance(root.get('content'), list):
        return root
    visible = {key: value for key, value in islice(root.items(), MAX_VISIBLE_NODES)
               if key not in _HIDDEN_FIELDS and isinstance(value, dict) and isinstance(value.get('content'), list)}
    selected = post_content_block(visible)
    return {} if _message_envelope(selected) else selected


# LLM: post 节点先守共用信封结构边界再调用原格式化；孤立 ID/正文词不拦，未核验信封的并列媒体也不能保留。
# 函数用途: 清理单个 post 显示节点，保留原代码、链接和资源格式并明确未知缺口。
def _post_node(node: object, scan: _VisibleScan) -> dict[str, str] | None:
    if not scan.admit(node, 3) or not isinstance(node, dict):
        if not isinstance(node, dict):
            scan.missing()
        return None
    if _message_envelope(node):
        scan.missing()
        return None
    safe = {key: node[key] for key in _POST_FIELDS if isinstance(node.get(key), str)}
    if 'image_key' not in safe and isinstance(node.get('img_key'), str):
        safe['image_key'] = node['img_key']
    if safe.get('tag') == 'at' and any(safe.get(key, '').strip() for key in ('text', 'content')):
        safe['tag'] = 'text'  # 只有已提供的可见标签可读，opaque user_id 不恢复成身份或正文。
    has_text = any(safe.get(key, '').strip() for key in ('text', 'content', 'href'))
    has_media = any(safe.get(key, '').strip() for key in ('image_key', 'file_key'))
    if not has_text and not has_media:
        if safe.get('tag') != 'hr':
            scan.missing()
        return None
    if safe.get('tag') not in _POST_TAGS:
        scan.partial = True
    scan.media(safe)
    return safe


# LLM: post 遍历与卡片共享节点预算；格式化只对过滤后的当前显示节点调用原 parser，不递归取子消息。
# 函数用途: 保留原 post 的标题、行顺序、代码和链接，返回完整或部分覆盖。
def _read_post(content: dict[str, Any], scan: _VisibleScan) -> None:
    post = _post_root(content)
    if not post:
        scan.missing()
        return
    rows = []
    for row in post['content']:
        if not scan.admit(row, 2):
            break
        if not isinstance(row, list):
            scan.missing()
            continue
        nodes = []
        for node in row:
            if scan.visited >= MAX_VISIBLE_NODES:
                scan.missing()
                break
            safe = _post_node(node, scan)
            if safe is not None:
                nodes.append(safe)
        rows.append(nodes)
    title = post.get('title') if isinstance(post.get('title'), str) else ''
    text, _ = extract_feishu_content('post', {'title': title, 'content': rows})
    scan.text(text)


# LLM: 已知文本/媒体优先调用原 parser，缺资源的旧占位不能冒充读到内容；媒体本体读取仍在主流程。
# 函数用途: 按已知消息协议抽取实际文字或资源，不下载附件、不依据扩展名拒绝未知文件。
def _read_known(msg_type: str, content: object, scan: _VisibleScan) -> None:
    if msg_type == 'text':
        visible = content.get('text') if isinstance(content, dict) else content
        if isinstance(visible, str):
            text, _ = extract_feishu_content('text', {'text': visible})
            scan.text(text)
        return
    if not isinstance(content, dict):
        scan.missing()
        return
    if msg_type == 'post':
        _read_post(content, scan)
        return
    fields = ('image_key',) if msg_type == 'image' else ('file_key', 'file_name')
    safe = {key: content[key] for key in fields if isinstance(content.get(key), str)}
    if msg_type == 'image' and 'image_key' not in safe and isinstance(content.get('img_key'), str):
        safe['image_key'] = content['img_key']
    scan.media(safe)
    if scan.items:
        text, _ = extract_feishu_content(msg_type, safe)
        scan.text(text)
    else:
        scan.missing()
        scan.text(safe.get('file_name'))


# LLM: 纯函数只投影已获准 parent 的内容，不证明身份/消息来源；未知协议永不称完整，字符截断仅由主线负责。
# 函数用途: 统一引用的可见文字、媒体引用和不可读取计数，供原消息准备流程安全使用。
def extract_quoted_content(msg_type: str, raw_content: object) -> QuotedContentProjection:
    content = _decode(raw_content)
    scan = _VisibleScan()
    if msg_type in _KNOWN_TYPES:
        _read_known(msg_type, content, scan)
    else:
        scan.partial = True
        scan.walk(content)
    text = '\n'.join(scan.parts)
    readable = bool(text.strip() or scan.items)
    coverage = 'unavailable' if not readable else 'visible_only' if scan.partial else 'complete'
    return QuotedContentProjection(text, coverage, scan.unreadable if readable else max(1, scan.unreadable),
        tuple(tuple(item.items()) for item in scan.items))

"""只复用已核验为空的普通canonical消息页；权限、活动及易失事件从不进入缓存。"""
from __future__ import annotations

import math
import os
import stat
import threading
import time
from collections import OrderedDict

from ..common.nofollow_fs import open_directory_beneath
from ..conversation.message_stream import read_background_response_page
from ..conversation.store import ConversationStore
from ..conversation.store_messages import MessageStore

# 进程内只保存空页和源版本，不保存通知正文；独立事件中心容量，默认ring=0同样生效。
_EMPTY_PAGE_CAPACITY = 512
_EMPTY_PAGES = OrderedDict()
_EMPTY_PAGES_LOCK = threading.Lock()


def _plain_message_source_stamp(store, thread_id):
    """限定已知原生读口；plain布局只依赖一个JSONL，冷热manifest出现即退出缓存。"""
    if type(store) is not ConversationStore or type(store.messages) is not MessageStore or store.messages.storage is not store.storage:
        return None
    path = store.storage.message_path(thread_id)
    descriptor = open_directory_beneath(store.storage.root, path.parent.relative_to(store.storage.root).parts, require_dir_fd=True)
    try:
        try:
            os.stat(path.with_suffix('.manifest.json').name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return None  # 分段/准备中/损坏清单都由原完整读口核验，不拿hot文件stamp替整个布局。
        parent = os.fstat(descriptor)
        try:
            info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            source = None
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
            if not os.access(path, os.R_OK):
                return None
            source = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)
        # 目录身份防重建/换代；源append改变size，规范rewrite/删除重建改变inode，原位变化改变mtime/ctime。
        return (parent.st_dev, parent.st_ino, parent.st_mode, source)
    finally:
        os.close(descriptor)


def read_idle_message_page(agent, store, thread_id, *, scope, after, display):
    """每次经原scope解析后才调用；只减省空消息页源读取，不复用整份通知响应。"""
    def read():
        return read_background_response_page(store, thread_id, after=after,
            include_foreground=display.foreground_messages, include_display_checkpoints=display.display_checkpoints)

    window = getattr(getattr(agent, 'config', None), 'gateway_idle_notice_reuse_seconds', 0)
    if type(window) not in (int, float) or not math.isfinite(window) or window <= 0:
        return read()
    try:
        before = _plain_message_source_stamp(store, thread_id)
    except (OSError, ValueError, TypeError, AttributeError):
        return read()
    if before is None:
        return read()
    key = (str(store.storage.root), thread_id, scope.user_id, scope.channel, scope.conversation_id,
           scope.all_user_access, after, display.foreground_messages, display.display_checkpoints)
    now = time.monotonic()
    with _EMPTY_PAGES_LOCK:
        cached = _EMPTY_PAGES.pop(key, None)
        if cached is not None and cached[0] == before and now - cached[1] < window:
            _EMPTY_PAGES[key] = cached
            return [], cached[2], True
    result = read()
    notices, cursor, ok = result
    if notices or not ok:
        return result
    try:
        after_stamp = _plain_message_source_stamp(store, thread_id)
    except (OSError, ValueError, TypeError, AttributeError):
        return result
    if before == after_stamp:
        with _EMPTY_PAGES_LOCK:
            _EMPTY_PAGES[key] = (before, now, cursor)
            while len(_EMPTY_PAGES) > _EMPTY_PAGE_CAPACITY:
                _EMPTY_PAGES.popitem(last=False)
    return result

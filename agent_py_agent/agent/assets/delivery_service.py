# LLM: 可信宿主根与调用方绑定Owner是权限上界；跨Owner只写投递箱/回执，正式资产仅本人accept写原canonical事务。
# 模块用途: 提供资产闭包投递、屏蔽、回执和持久待消费通知；队列入箱不代表真实通道已经送达。
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import astuple, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    list_names_beneath,
    open_directory_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_bytes_atomic_beneath,
)
from ..user_space.local_user_store import LocalUserStore
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from . import safe_asset_id
from .delivery_contracts import (
    BLOCKLIST_SCHEMA,
    ENVELOPE_SCHEMA,
    MAX_BUNDLE_BYTES,
    MAX_PENDING_DELIVERY_COUNT,
    RECEIPT_SCHEMA,
    AssetDeliveryError,
    DeliverySendOptions,
)
from .delivery_sanitizer import datasource_secret_values, sanitize_bundle_files
from .script_store_support import (
    commit_delivery_items,
    delivery_item_manifest,
    delivery_json,
    delivery_scheduler,
    new_delivery_monitor_job,
    prepare_delivery_items,
    required_delivery_capabilities,
    snapshot_delivery_bundle,
)

_BASE = ('data', 'asset_delivery')


# LLM: 仅产生UTC展示时间，不用时间文本建立身份或幂等权限。
# 函数用途: 为包裹和回执记录本次状态时间。
def _timestamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


# LLM: Owner字符串必须完整匹配规范命名域，单段校验不允许路径穿越；身份只供可信调用方解析，不认证角色。
# 函数用途: 还原规范本地或通道用户身份，不推断当前用户。
def _identity(owner_id):
    if not isinstance(owner_id, str):
        raise ValueError('owner_id必须是规范字符串')
    parts = owner_id.split('/')
    for part in parts:
        safe_asset_id(part)
    if parts == ['local', 'main']:
        return OwnerIdentity.local_main()
    if len(parts) == 2 and parts[0] == 'local':
        return OwnerIdentity.local_user(parts[1])
    if len(parts) == 4 and parts[0] == 'providers' and parts[2] == 'users':
        return OwnerIdentity.provider_user(parts[1], parts[3])
    raise ValueError('owner_id必须是本地或通道用户规范编号')


# LLM: 成功摘要只包含清单，不回传payload字节；计数和总大小从实际冻结文件生成。
# 函数用途: 为预览、入箱及通知共用同一包裹摘要。
def _bundle_summary(items):
    result = {kind + 's_count': sum(item['descriptor'].kind == kind for item in items)
        for kind in ('workflow', 'monitor', 'script', 'skill')}
    return {**result, 'total_assets_count': len(items),
        'total_bytes': sum(len(raw) for item in items for raw in item['files'].values())}


# LLM: 纯读取宿主构造的包裹身份，不从备注取得通道或目标；幂等键只依赖投递及接收方，queued不是外部送达证据。
# 函数用途: 为本地或通道用户构造统一的持久待消费通知，不发请求或新建会话。
def _delivery_notification(envelope):
    identifier, sender, recipient = (envelope[key] for key in ('delivery_id', 'sender_owner_id', 'recipient_owner_id'))
    root = envelope['root_asset']
    identity = _identity(recipient)
    event = {'schema_version': 'asset_delivery_notification.v1', 'delivery_id': identifier,
        'sender_owner_id': sender, 'recipient_owner_id': recipient, 'channel': identity.provider,
        'idempotency_key': f'asset_delivery:{identifier}:{recipient}', 'status': 'queued',
        'created_at': envelope['created_at'],
        'text': f'收到来自{sender}的资产投递：{root["kind"]}:{root["id"]}。回复 /delivery accept {identifier} 接收（默认关闭），或 /delivery reject {identifier} 拒绝。'}
    if identity.provider == 'feishu':
        event['recipient_open_id'] = recipient.rsplit('/', 1)[-1]
    return event


# LLM: quota由外层持有，继续取原资产根/绑定/scheduler锁；启停原账本并沿原事件退代、等待运行取消，不新建平行调度器。
# 函数用途: 同时启停监控绑定与真实Scheduler任务，写失败恢复旧绑定和旧调度状态。
def _toggle_delivery_monitor(home, owner_id, identifier, enabled, admission):
    from ..scheduler.event_source_lifecycle import cancel_retired_event_runs
    from ..scheduler.event_sources import sync_event_sources
    from ..scheduler.monitor_state import resume_monitor
    from ..scheduler.repository import _resume_next_run
    from . import asset_root_lock
    from .descriptor import AssetDescriptor
    from .monitor_adapter import get_monitor_descriptor
    from .script_store_support import _raw_object
    from .workspace_assets import _DESCRIPTOR_FIELDS

    parts = ('data', 'monitors', identifier + '.json')
    with asset_root_lock(home):
        binding = _raw_object(home, parts)
        raw_store = _raw_object(home, ('data', 'scheduler', 'store.json')) or {}
        if binding is None and identifier not in raw_store.get('jobs', {}):
            raise AssetDeliveryError('ASSET_NOT_FOUND', '目标资产不存在')
        os.close(open_directory_beneath(home, parts[:-1], create=True, require_dir_fd=True))
        repo = delivery_scheduler(home, owner_id)
        with locked_json_path(home.joinpath(*parts)), locked_json_path(repo.store_path):
            store, now = repo._load_store_unlocked(), time.time()
            previous = deepcopy(store)
            job = store['jobs'].get(identifier)
            if job is not None and job['status'] == 'deleted':
                raise AssetDeliveryError('ASSET_NOT_FOUND', '目标资产不存在')
            if job is None:
                descriptor = AssetDescriptor.from_dict({key: value for key, value in binding.items() if key in _DESCRIPTOR_FIELDS})
                descriptor = replace(descriptor, owner_id=owner_id, enabled=enabled)
                job = new_delivery_monitor_job(repo, descriptor, binding, store)
            elif enabled and job.get('rules'):
                job = resume_monitor(job, now)
            updated = {**job, 'enabled': enabled, 'status': 'active' if enabled else 'paused',
                'paused_reason': '' if enabled else 'asset_disabled_by_owner',
                'paused_at': 0.0 if enabled else now, 'updated_at': now, 'version': int(job['version']) + 1}
            if enabled and not job.get('rules'):
                updated['next_run_at'] = _resume_next_run(job, now)
            updated = sync_event_sources(updated, job, retire=True)
            store['jobs'][identifier] = cancel_retired_event_runs(store, updated, now=now)
            cancelled = repo._cancel_waiting_runs_unlocked(store, identifier, now, 'job_paused') if not enabled else []
            old_binding = read_bytes_beneath(home, parts, max_bytes=MAX_BUNDLE_BYTES, require_dir_fd=True)
            try:
                if binding is not None:
                    binding = {**binding, 'enabled': enabled, 'revision': int(binding.get('revision', 0)) + 1,
                        'schedule_spec': {**binding.get('schedule_spec', {}), 'enabled': enabled}}
                    raw = delivery_json(binding)
                    admission.check((OwnerQuotaChange(home.joinpath(*parts), len(raw)),))
                    write_bytes_atomic_beneath(home, parts, raw)
                repo._write_store_unlocked(store, admission, history_records=cancelled)
            except BaseException:
                if old_binding is not None:
                    write_bytes_atomic_beneath(home, parts, old_binding)
                repo._write_store_unlocked(previous, admission)
                raise
            return {**get_monitor_descriptor(home, identifier), 'enabled': enabled}


# LLM: 所有读写仍沿同一可信root、nofollow及原quota锁；拆分存储职责不增加身份、路径或第二账本。
# 类用途: 为投递领域动作集中提供受限JSON读写与两端写入准入。
class _DeliveryStorage:
    # LLM: 调用方必须持接收方delivery锁；仅回收未发表UUID暂存，FD安全rmtree不可用时失败关闭，不触碰正式箱。
    # 函数用途: 在显式发送时清理崩溃留下的暂存目录。
    def _clean_staging(self, home):
        parts = (*home.relative_to(self.root_dir).parts, *_BASE, 'staging')
        names = list_names_beneath(self.root_dir, parts)
        if not names:
            return
        if not shutil.rmtree.avoids_symlink_attacks:
            raise AssetDeliveryError('DELIVERY_RECOVERY_REQUIRED', '当前平台不能安全清理投递暂存')
        parent = open_directory_beneath(self.root_dir, parts, require_dir_fd=True)
        try:
            for name in names:
                if re.fullmatch('[0-9a-f]{32}', name) is None:
                    raise AssetDeliveryError('DELIVERY_STORE_CORRUPT', '投递暂存目录包含非协议成员')
                shutil.rmtree(name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    # LLM: 调用方已持接收方配额/投递锁；queued只对已发表inbox可消费，崩溃prepared由同一箱事实决定补发或回收。
    # 函数用途: 在显式发送或消费时对账中断/终态通知，不在只读查询写盘或触碰其他发送者回执。
    def _recover_notifications(self, home, owner_id, admission):
        for name in list_names_beneath(home, (*_BASE, 'notifications')):
            if not name.endswith('.json'):
                continue
            identifier = safe_asset_id(name[:-5])
            parts = (*_BASE, 'notifications', name)
            event = self._read_json(home, parts)
            if (event is None or event.get('schema_version') != 'asset_delivery_notification.v1'
                    or event.get('delivery_id') != identifier or event.get('recipient_owner_id') != owner_id):
                raise AssetDeliveryError('DELIVERY_STORE_CORRUPT', '通知记录身份或格式不匹配')
            stored = self._read_json(home, (*_BASE, 'inbox', identifier, 'envelope.json'))
            if stored is None:
                if event.get('status') in {'prepared', 'queued'}:
                    unlink_file_beneath(self.root_dir, (*home.relative_to(self.root_dir).parts, *parts))
            elif stored.get('status') in {'accepted', 'rejected'}:
                self._resolve_notification(home, self._envelope(home, owner_id, identifier), admission)
            elif event.get('status') == 'prepared':
                envelope = self._envelope(home, owner_id, identifier)
                event = self._inbox_notification(home, envelope)['notification_event']
                self._put_json(home, parts, {**event, 'status': 'queued'}, admission)

    # LLM: 终态只能取已核包裹；保留既有dispatch时间，缺记录从可信身份重建，不把queued留在已结算包上。
    # 函数用途: 将接受或拒绝结果持久同步到接收方通知。
    def _resolve_notification(self, home, envelope, admission):
        view = self._inbox_notification(home, envelope)
        event = (_delivery_notification(envelope) if view['notification_status'] == 'missing'
            else view['notification_event'])
        event = {**event, 'status': envelope['status'], 'resolved_at': envelope['updated_at']}
        self._put_json(home, (*_BASE, 'notifications', envelope['delivery_id'] + '.json'), event, admission)

    # LLM: 仅当前接收方quota→delivery锁下领取queued；返回dispatched是耐久消费凭据，不代表实际IM发送或用户可见。
    # 函数用途: 对账中断后按通道和有界数量消费生产通知队列，同条记录不重复领取。
    def consume_notifications(self, recipient_owner_id: str, *, channel: str | None = None,
                              limit: int = 50) -> dict[str, Any]:
        if channel is not None and (not isinstance(channel, str) or not channel):
            raise ValueError('channel必须是非空字符串或None')
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('limit必须是1到500的整数')
        home, dispatched = self._home(recipient_owner_id), []
        with self._write_scope((home,)) as admissions:
            self._recover_notifications(home, recipient_owner_id, admissions[home])
            for name in sorted(list_names_beneath(home, (*_BASE, 'notifications'))):
                if not name.endswith('.json'):
                    continue
                identifier = safe_asset_id(name[:-5])
                envelope = self._envelope(home, recipient_owner_id, identifier)
                event = self._inbox_notification(home, envelope)['notification_event']
                if event['status'] != 'queued' or channel is not None and event.get('channel') != channel:
                    continue
                event = {**event, 'status': 'dispatched', 'dispatched_at': _timestamp()}
                self._put_json(home, (*_BASE, 'notifications', name), event, admissions[home])
                dispatched.append(event)
                if len(dispatched) >= limit:
                    break
        return {'ok': True, 'notifications': dispatched}

    # LLM: 队列身份、接收通道和幂等键必须与已核包裹重建值一致；不以旧内嵌事件冒充入队，冷读不补写。
    # 函数用途: 让收件箱消费者看到当前真实通知状态，缺记录时明确标成missing。
    def _inbox_notification(self, home, envelope):
        event = self._read_json(home, (*_BASE, 'notifications', envelope['delivery_id'] + '.json'))
        if event is None:
            return {**envelope, 'notification_status': 'missing'}
        expected = _delivery_notification(envelope)
        keys = ('delivery_id', 'sender_owner_id', 'recipient_owner_id', 'channel', 'idempotency_key', 'recipient_open_id')
        if (event.get('schema_version') != 'asset_delivery_notification.v1'
                or any(event.get(key) != expected.get(key) for key in keys)
                or not isinstance(event.get('status'), str) or not event['status']):
            raise AssetDeliveryError('DELIVERY_STORE_CORRUPT', '通知记录身份或格式不匹配')
        return {**envelope, 'notification_status': event['status'], 'notification_event': event}

    # LLM: 只从规范root-relative文件读取JSON，不把坏记录降为缺失；错误不包含原正文。
    # 函数用途: 为箱、名单和回执统一读取受限JSON。
    def _read_json(self, home, parts):
        raw = read_bytes_beneath(self.root_dir, (*home.relative_to(self.root_dir).parts, *parts),
            max_bytes=MAX_BUNDLE_BYTES, require_dir_fd=True)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('not object')
            return value
        except (ValueError, RecursionError) as exc:
            raise AssetDeliveryError('DELIVERY_STORE_CORRUPT', '投递记录损坏') from exc

    # LLM: 配额准入必须在领域写锁之前；完整JSON只走root-relative nofollow原子写，不新增账号或另建授权账。
    # 函数用途: 安全发表一个投递状态对象。
    def _put_json(self, home, parts, value, admission):
        raw = delivery_json(value)
        admission.check((OwnerQuotaChange(home.joinpath(*parts), len(raw)),))
        write_bytes_atomic_beneath(self.root_dir, (*home.relative_to(self.root_dir).parts, *parts), raw)

    # LLM: 所有Owner先按路径排序取得quota，再按同顺序取领域锁，避免交叉互投死锁；只供显式写调用。
    # 函数用途: 串行化投递状态变更并保留统一配额锁序。
    @contextmanager
    def _write_scope(self, homes):
        homes = sorted(set(homes))
        with ExitStack() as stack:
            admissions = {home: stack.enter_context(owner_quota_enforcer_from_policy(home).admission()) for home in homes}
            for home in homes:
                os.close(open_directory_beneath(self.root_dir, (*home.relative_to(self.root_dir).parts, *_BASE), create=True, require_dir_fd=True))
                stack.enter_context(locked_json_path(home.joinpath(*_BASE, '.delivery')))
            yield admissions


# LLM: 调用方必须冻结真实effective_owner，模型参数不能指定首个Owner或admin_provision；只读入口不物化存储。
# 类用途: 为Host v2、管理预置、Agent工具和斜杠命令提供同一跨Owner投递服务。
class AssetDeliveryService(_DeliveryStorage):
    # LLM: 仅规范化可信宿主根，不创建Owner、锁、目录或调度器。
    # 函数用途: 绑定整套用户目录所在的宿主根。
    def __init__(self, root_dir):
        self.root_dir = Path(root_dir).resolve()
        self.users = LocalUserStore(self.root_dir)

    # LLM: 本地账号沿原账号状态/目录身份权威，通道用户沿实际目录；不把发现当作角色或执行授权。
    # 函数用途: 返回排除本人后的可投递用户，不创建任何存储。
    def list_targets(self, sender_owner_id):
        self._home(sender_owner_id)
        candidates = [('local/main', '本地管理员')]
        candidates.extend((f'local/{user.user_id}', user.display_name) for user in self.users.list_users() if user.status == 'enabled')
        candidates.extend((f'providers/feishu/users/{name}', name)
            for name in list_names_beneath(self.root_dir, ('owners', 'providers', 'feishu', 'users')))
        targets = []
        for owner_id, display_name in candidates:
            if owner_id == sender_owner_id:
                continue
            self._home(owner_id)
            targets.append({'owner_id': owner_id, 'display_name': display_name,
                'blocked_by_target': sender_owner_id in self.get_blocklist(owner_id)['blocked_owners']})
        return {'ok': True, 'targets': targets}

    # LLM: 复用发送同一原始字节冻结/扫描入口；只返回清单与需求，不创建锁或暴露源代码。
    # 函数用途: 预览一个资产及其完整依赖包裹。
    def preview_bundle(self, sender_owner_id, root_asset_kind, root_asset_id, include_dependencies=True):
        home = self._home(sender_owner_id)
        items = snapshot_delivery_bundle(home, root_asset_kind, root_asset_id, include_dependencies=include_dependencies)
        return {'ok': True, 'root_asset': {'kind': root_asset_kind, 'id': root_asset_id},
            'bundled_assets': [delivery_item_manifest(item, f'{root_asset_kind}:{root_asset_id}') for item in items],
            'closure_summary': _bundle_summary(items), 'required_capabilities_summary': required_delivery_capabilities(items)}

    # LLM: 原发送参数及管理员边界保持；备注和原文件先核已配置秘密，完成持久入队才报告queued，不声称外部送达。
    # 函数用途: 发布包裹、发件回执及接收方可消费通知，管理预置仍可显式接受。
    def send_bundle(self, sender_owner_id, recipient_owner_id, root_asset_kind, root_asset_id, *positional, **options):
        note, include_dependencies, allow_secret_placeholder_replace, auto_accept, auto_enable, sender_role = astuple(
            DeliverySendOptions.from_call(positional, options))
        sender, recipient = self._home(sender_owner_id), self._home(recipient_owner_id)
        if sender_owner_id == recipient_owner_id:
            raise AssetDeliveryError('DELIVERY_TARGET_INVALID', '投递目标不能是本人')
        if sender_role not in {'user', 'admin_provision'}:
            raise ValueError('未知sender_role')
        provision = sender_role == 'admin_provision' and sender_owner_id == 'local/main'
        if sender_role == 'admin_provision' and not provision or (auto_accept or auto_enable) and not provision:
            raise AssetDeliveryError('DELIVERY_ADMIN_PROVISION_REQUIRED', '自动接收仅允许可信管理员预置')
        if type(auto_accept) is not bool or type(auto_enable) is not bool or auto_enable and not auto_accept:
            raise ValueError('auto_accept和auto_enable必须是合法布尔组合')
        self._check_blocked(recipient_owner_id, sender_owner_id, provision)
        if not isinstance(note, str) or len(note) > 4000:
            raise ValueError('note必须是最多4000字符的文本')
        safe_note = sanitize_bundle_files({'note.txt': note.encode()}, owner_home=sender,
            extra_secrets=datasource_secret_values(sender),
            allow_secret_placeholder_replace=allow_secret_placeholder_replace)['note.txt'].decode()
        items = snapshot_delivery_bundle(sender, root_asset_kind, root_asset_id,
            include_dependencies=include_dependencies, allow_secret_placeholder_replace=allow_secret_placeholder_replace)
        identifier = 'dlv_' + uuid.uuid4().hex
        envelope = self._new_envelope(identifier, sender_owner_id, recipient_owner_id, root_asset_kind, root_asset_id, safe_note, items)
        with self._write_scope((sender, recipient)) as admissions:
            self._home(sender_owner_id)
            self._home(recipient_owner_id)
            self._check_blocked(recipient_owner_id, sender_owner_id, provision)
            pending = self.list_inbox(recipient_owner_id, 'pending')['deliveries']
            if len(pending) >= MAX_PENDING_DELIVERY_COUNT:
                raise AssetDeliveryError('ASSET_DELIVERY_QUOTA_EXCEEDED', '待确认投递已达上限')
            self._publish(sender, recipient, envelope, items, admissions)
        result = {'ok': True, 'delivery_id': identifier, 'status': 'pending',
            'notification_status': 'queued', 'notification_event': envelope['notification_event']}
        if auto_accept:
            try:
                result['accept_result'] = self.accept_delivery(recipient_owner_id, identifier, initial_enabled=auto_enable)
                result['status'] = 'accepted'
            except (AssetDeliveryError, OSError, ValueError) as exc:
                result.update(ok=False, error_code=getattr(exc, 'error_code', 'DELIVERY_AUTO_ACCEPT_FAILED'), committed=True)
        return result

    # LLM: 有界nofollow读包裹及当前耐久通知，不取写锁或补写队列；坏schema/身份不隐藏成空箱。
    # 函数用途: 按状态读取本人收件箱并向通知消费者暴露真实待处理状态。
    def list_inbox(self, recipient_owner_id, status_filter=None):
        home = self._home(recipient_owner_id)
        if status_filter not in {None, 'all', 'pending', 'accepted', 'rejected'}:
            raise ValueError('未知投递状态过滤器')
        deliveries = []
        for identifier in sorted(list_names_beneath(home, (*_BASE, 'inbox'))):
            envelope = self._envelope(home, recipient_owner_id, identifier)
            if status_filter in {None, 'all'} or envelope['status'] == status_filter:
                deliveries.append(self._inbox_notification(home, envelope))
        return {'ok': True, 'deliveries': deliveries}

    # LLM: 首个Owner仅由可信调用方绑定；每文件和发件清单hash先验，再沿原quota/资产事务安装，正常接受默认全关闭。
    # 函数用途: 原子接收整包资产，统一冲突重命名、来源记录及两端回执。
    def accept_delivery(self, recipient_owner_id, delivery_id, conflict_strategy='keep_both_rename', initial_enabled=False):
        if conflict_strategy not in {'keep_both_rename', 'overwrite', 'skip'} or type(initial_enabled) is not bool:
            raise ValueError('接收冲突策略或initial_enabled无效')
        recipient = self._home(recipient_owner_id)
        preliminary = self._envelope(recipient, recipient_owner_id, delivery_id)
        sender = self._home(preliminary['sender_owner_id'])
        with self._write_scope((sender, recipient)) as admissions:
            envelope = self._envelope(recipient, recipient_owner_id, delivery_id)
            receipt = self._origin_receipt(sender, envelope)
            if envelope['status'] == 'accepted':
                return self._resolve_delivery(sender, recipient, envelope, receipt, 'accepted', admissions)
            if receipt['status'] == 'rejected':
                envelope['resolution'] = receipt.get('resolution', {})
                self._resolve_delivery(sender, recipient, envelope, receipt, 'rejected', admissions)
                raise AssetDeliveryError('DELIVERY_STATE_CONFLICT', '投递已拒绝，不能接收')
            if receipt['status'] == 'accepted':
                envelope['resolution'] = receipt.get('resolution', {})
                return self._resolve_delivery(sender, recipient, envelope, receipt, 'accepted', admissions)
            if envelope['status'] != 'pending':
                self._resolve_notification(recipient, envelope, admissions[recipient])
                raise AssetDeliveryError('DELIVERY_STATE_CONFLICT', '投递已拒绝，不能接收')
            payloads = self._payloads(recipient, envelope)
            if self._installation_matches(recipient, envelope):
                return self._resolve_delivery(sender, recipient, envelope, receipt, 'accepted', admissions)
            if envelope.get('resolution', {}).get('expected_files') and not self._installation_matches(recipient, envelope, previous=True):
                raise AssetDeliveryError('DELIVERY_INSTALLATION_STATE_CHANGED', '未结算的安装目标已变动，不能自动重装或覆盖')
            accepted_at = _timestamp()
            drafts, extras, installed = prepare_delivery_items(recipient, envelope, payloads,
                conflict_strategy, initial_enabled, accepted_at)
            envelope['resolution'] = {'installed_assets': installed, 'conflict_strategy': conflict_strategy,
                'initial_enabled': initial_enabled, 'accepted_at': accepted_at, 'phase': 'installing'}

            # LLM: 只保存路径/hash而不保存第二份代码，正式写前耐久记录，供提交后状态写失败的精确重试辨识。
            # 函数用途: 记录即将原子发表的canonical叶清单。
            def prepared(expected):
                envelope['resolution']['expected_files'] = expected
                self._put_json(recipient, (*_BASE, 'inbox', delivery_id, 'envelope.json'), envelope, admissions[recipient])

            commit_delivery_items(recipient, drafts, extras, admissions[recipient], prepared)
            return self._resolve_delivery(sender, recipient, envelope, receipt, 'accepted', admissions)

    # LLM: 仅本人可拒绝待确认包裹；接受后不得用reject回滚资产，重复拒绝不再次改变正式资产。
    # 函数用途: 拒绝本人投递并同步发送方回执，不安装任何文件。
    def reject_delivery(self, recipient_owner_id, delivery_id, reason=''):
        recipient = self._home(recipient_owner_id)
        preliminary = self._envelope(recipient, recipient_owner_id, delivery_id)
        sender = self._home(preliminary['sender_owner_id'])
        if not isinstance(reason, str) or len(reason) > 4000:
            raise ValueError('reason必须是最多4000字符的文本')
        safe_reason = sanitize_bundle_files({'reason.txt': reason.encode()}, owner_home=recipient)['reason.txt'].decode()
        with self._write_scope((sender, recipient)) as admissions:
            envelope = self._envelope(recipient, recipient_owner_id, delivery_id)
            receipt = self._origin_receipt(sender, envelope)
            if receipt['status'] == 'accepted' or envelope['status'] == 'accepted' or self._installation_matches(recipient, envelope):
                envelope['resolution'] = receipt.get('resolution') or envelope.get('resolution', {})
                self._resolve_delivery(sender, recipient, envelope, receipt, 'accepted', admissions)
                raise AssetDeliveryError('DELIVERY_STATE_CONFLICT', '资产已安装，不能再拒绝')
            if receipt['status'] == 'rejected' or envelope['status'] == 'rejected':
                envelope['resolution'] = receipt.get('resolution') or envelope.get('resolution', {})
                return self._resolve_delivery(sender, recipient, envelope, receipt, 'rejected', admissions)
            envelope['resolution'] = {'reason': safe_reason}
            return self._resolve_delivery(sender, recipient, envelope, receipt, 'rejected', admissions)

    # LLM: 只切换当前本人原canonical资产；原Store缺失转换领域404，不把KeyError当未知副作用，工具层仍负责启用审批。
    # 函数用途: 开启或关闭本人已经接收的某一资产。
    def toggle_asset(self, owner_id, asset_kind, asset_id, enabled):
        home = self._home(owner_id)
        safe_asset_id(asset_id)
        if type(enabled) is not bool:
            raise ValueError('enabled必须是布尔值')
        with self._write_scope((home,)) as admissions:
            try:
                descriptor = self._toggle_canonical(home, asset_kind, asset_id, enabled, admissions[home])
            except (KeyError, FileNotFoundError):
                raise AssetDeliveryError('ASSET_NOT_FOUND', f'资产不存在: {asset_kind}:{asset_id}') from None
        return {'ok': True, 'asset': descriptor, 'enabled': enabled}

    # LLM: 领域方法共享外层准入，不重复取不可重入quota锁；监控必须同步原绑定和真实Scheduler，不能只改展示标签。
    # 函数用途: 在当前Owner原canonical存储上执行启停。
    def _toggle_canonical(self, home, kind, identifier, enabled, admission):
        from .script_store import ScriptStore
        from .skill_adapter import SkillAdapter, get_owner_skill_descriptor
        from .workflow_store import WorkflowStore

        if kind == 'script':
            return ScriptStore(home).set_enabled(identifier, enabled).to_dict()
        if kind == 'workflow':
            return WorkflowStore(home).set_enabled(identifier, enabled).descriptor.to_dict()
        if kind == 'skill':
            get_owner_skill_descriptor(home, identifier)
            return SkillAdapter(home).set_enabled(identifier, enabled, admission=admission)
        if kind == 'monitor':
            owner_id = home.relative_to(self.root_dir / 'owners').as_posix()
            return _toggle_delivery_monitor(home, owner_id, identifier, enabled, admission)
        raise ValueError('未知资产kind')

    # LLM: 缺失名单返回协议空值，不物化目录或锁；既有坏记录失败关闭，不能默认为未屏蔽。
    # 函数用途: 读取本人投递屏蔽名单。
    def get_blocklist(self, owner_id):
        home = self._home(owner_id)
        value = self._read_json(home, (*_BASE, 'blocklist.json'))
        if value is None:
            return {'ok': True, 'schema_version': BLOCKLIST_SCHEMA, 'blocked_owners': []}
        if value.get('schema_version') != BLOCKLIST_SCHEMA or not isinstance(value.get('blocked_owners'), list):
            raise AssetDeliveryError('DELIVERY_STORE_CORRUPT', '屏蔽名单格式不正确')
        for target in value['blocked_owners']:
            _identity(target)
        return {'ok': True, **value}

    # LLM: 仅修改本人名单，目标ID只做严格身份解析；原quota→领域锁顺序，不要求被屏蔽人仍在线或启用。
    # 函数用途: 幂等添加或移除投递发送方屏蔽。
    def update_blocklist(self, owner_id, operation, target_owner_id):
        home = self._home(owner_id)
        _identity(target_owner_id)
        if operation not in {'add', 'remove'}:
            raise ValueError('operation必须是add或remove')
        with self._write_scope((home,)) as admissions:
            previous = self.get_blocklist(owner_id)
            owners = set(previous['blocked_owners'])
            owners.add(target_owner_id) if operation == 'add' else owners.discard(target_owner_id)
            value = {'schema_version': BLOCKLIST_SCHEMA, 'owner_id': owner_id,
                'blocked_owners': sorted(owners), 'updated_at': _timestamp()}
            self._put_json(home, (*_BASE, 'blocklist.json'), value, admissions[home])
        return {'ok': True, **value}

    # LLM: 规范身份经原resolver定位，本地用户再核账号/目录身份；只打开既有目录且不赋予新角色。
    # 函数用途: 验证一个真实且启用的用户目录，禁止向未注册Owner写入。
    def _home(self, owner_id):
        identity = _identity(owner_id)
        result = resolve_owner_home(self.root_dir, identity)
        try:
            if identity.provider == 'local' and identity.owner_kind == 'user':
                self.users.require_enabled(identity.owner_id)
            os.close(open_directory_beneath(self.root_dir, result.home_dir.relative_to(self.root_dir).parts, require_dir_fd=True))
        except (OSError, ValueError) as exc:
            raise AssetDeliveryError('DELIVERY_TARGET_NOT_FOUND', '投递用户不存在或不可用') from exc
        if result.owner_id != owner_id:
            raise ValueError('Owner身份不规范')
        return result.home_dir

    # LLM: 仅可信admin_provision可绕过屏蔽，普通发送永不先入箱或先生成通知。
    # 函数用途: 检查接收方是否拒收该发送方。
    def _check_blocked(self, recipient, sender, provision):
        if not provision and sender in self.get_blocklist(recipient)['blocked_owners']:
            raise AssetDeliveryError('DELIVERY_BLOCKED_BY_RECIPIENT', '接收方已屏蔽此发送方')

    # LLM: 包裹ID/Owner/协议均须匹配固定位置，不能凭可改正文重定位后续回执或安装根。
    # 函数用途: 读取并核验一份本人包裹。
    def _envelope(self, home, recipient, identifier):
        safe_asset_id(identifier)
        value = self._read_json(home, (*_BASE, 'inbox', identifier, 'envelope.json'))
        if value is None:
            raise AssetDeliveryError('DELIVERY_NOT_FOUND', '投递不存在')
        if (value.get('schema_version') != ENVELOPE_SCHEMA or value.get('delivery_id') != identifier
                or value.get('recipient_owner_id') != recipient or value.get('status') not in {'pending', 'accepted', 'rejected'}):
            raise AssetDeliveryError('DELIVERY_INTEGRITY_FAILED', '包裹身份或格式不匹配')
        return value

    # LLM: 通知绑定包裹及两端Owner，幂等键稳定；queued仅表示待消费，不新建会话或声称外部已送达。
    # 函数用途: 为本地和通道接收方生成包裹及耐久通知事件。
    def _new_envelope(self, identifier, sender, recipient, kind, asset_id, note, items):
        value = {'schema_version': ENVELOPE_SCHEMA, 'delivery_id': identifier, 'sender_owner_id': sender,
            'recipient_owner_id': recipient, 'root_asset': {'kind': kind, 'id': asset_id}, 'note': note,
            'created_at': _timestamp(), 'updated_at': _timestamp(), 'status': 'pending',
            'closure_summary': _bundle_summary(items), 'required_capabilities_summary': required_delivery_capabilities(items),
            'bundled_assets': [delivery_item_manifest(item, f'{kind}:{asset_id}') for item in items]}
        value['notification_event'] = _delivery_notification(value)
        return value

    # LLM: 同锁先耐久prepared再发表箱，最后提升queued；中断后依据inbox对账，未发表通知绝不可消费。
    # 函数用途: 在入箱提交后才发表可消费通知，异常保留可核状态而不伪称整次未写入。
    def _publish(self, sender, recipient, envelope, items, admissions):
        identifier = envelope['delivery_id']
        size = len(delivery_json(envelope)) + envelope['closure_summary']['total_bytes']
        if size > MAX_BUNDLE_BYTES:
            raise AssetDeliveryError('ASSET_DELIVERY_QUOTA_EXCEEDED', '投递包超过字节上限')
        stage = (*_BASE, 'staging', uuid.uuid4().hex)
        inbox = (*_BASE, 'inbox')
        receipt = {key: value for key, value in envelope.items() if key not in {'bundled_assets', 'notification_event'}}
        receipt.update(schema_version=RECEIPT_SCHEMA, bundle_sha256=sha256(delivery_json(envelope['bundled_assets'])).hexdigest())
        receipt_parts = (*_BASE, 'outbox', identifier + '.json')
        notification_parts = (*_BASE, 'notifications', identifier + '.json')
        admissions[sender].check((OwnerQuotaChange(sender.joinpath(*receipt_parts), len(delivery_json(receipt))),))
        self._recover_notifications(recipient, envelope['recipient_owner_id'], admissions[recipient])
        self._clean_staging(recipient)
        changes = [OwnerQuotaChange(recipient.joinpath(*stage, 'quota-projection'), size * 3)]
        admissions[recipient].check(changes)
        prefix = recipient.relative_to(self.root_dir).parts
        os.close(open_directory_beneath(self.root_dir, (*prefix, *stage), create=True, require_dir_fd=True))
        published = False
        try:
            self._stage_payload(prefix, stage, items)
            self._put_json(recipient, (*stage, 'envelope.json'), envelope, admissions[recipient])
            self._put_json(sender, receipt_parts, receipt, admissions[sender])
            self._put_json(recipient, notification_parts, {**envelope['notification_event'], 'status': 'prepared'}, admissions[recipient])
            self._publish_stage(prefix, stage, inbox, identifier)
            published = True
            self._put_json(recipient, notification_parts, envelope['notification_event'], admissions[recipient])
        finally:
            if not published:
                # rename成功后fsync失败仍可能已入箱，不能删掉该包的真实来源回执。
                installed = self._read_json(recipient, (*inbox, identifier, 'envelope.json'))
                if installed is None:
                    self._clean_staging(recipient)
                    unlink_file_beneath(self.root_dir, (*sender.relative_to(self.root_dir).parts, *receipt_parts))
                    unlink_file_beneath(self.root_dir, (*prefix, *notification_parts))

    # LLM: 全部内容已经扫描冻结，路径只从安全kind/id/资源组件形成；不在写入中重读发送方文件。
    # 函数用途: 写完本次同文件系统暂存中的所有负载。
    def _stage_payload(self, prefix, stage, items):
        for item in items:
            descriptor = item['descriptor']
            for name, raw in item['files'].items():
                parts = (*prefix, *stage, 'payload', descriptor.kind, descriptor.id, *name.split('/'))
                write_bytes_atomic_beneath(self.root_dir, parts, raw)

    # LLM: 两端父目录nofollow打开后立即登记清理，第二次open失败也关闭首个FD；发表失败仍由上层核实提交事实。
    # 函数用途: 无描述符泄漏地将同文件系统暂存原子改名为正式收件包。
    def _publish_stage(self, prefix, stage, inbox, identifier):
        with ExitStack() as stack:
            source = open_directory_beneath(self.root_dir, (*prefix, *stage[:-1]), require_dir_fd=True)
            stack.callback(os.close, source)
            target = open_directory_beneath(self.root_dir, (*prefix, *inbox), create=True, require_dir_fd=True)
            stack.callback(os.close, target)
            os.replace(stage[-1], identifier, src_dir_fd=source, dst_dir_fd=target)
            os.fsync(target)

    # LLM: 发件人回执是发送时冻结的来源证据，接收方可改箱不能伪造另一个Owner的来源或清单，状态更新不改原hash。
    # 函数用途: 将本人包裹与真实发送方原回执逐项绑定。
    def _origin_receipt(self, sender, envelope):
        receipt = self._read_json(sender, (*_BASE, 'outbox', envelope['delivery_id'] + '.json'))
        digest = sha256(delivery_json(envelope.get('bundled_assets'))).hexdigest()
        if (receipt is None or receipt.get('schema_version') != RECEIPT_SCHEMA or receipt.get('status') not in {'pending', 'accepted', 'rejected'}
                or any(receipt.get(key) != envelope.get(key) for key in ('delivery_id', 'sender_owner_id', 'recipient_owner_id', 'root_asset'))
                or receipt.get('bundle_sha256') != digest):
            raise AssetDeliveryError('DELIVERY_INTEGRITY_FAILED', '包裹与发件来源清单不一致')
        return receipt

    # LLM: 清单路径只允许原资源相对路径和安全kind/id，每字节重新核size/hash；校验完全部文件前绝不安装。
    # 函数用途: 从本人箱中加载并验证完整冻结负载。
    def _payloads(self, home, envelope):
        from .workspace_assets import _resource_parts

        result, total = {}, 0
        assets = envelope.get('bundled_assets')
        if not isinstance(assets, list) or not assets:
            raise AssetDeliveryError('DELIVERY_INTEGRITY_FAILED', '包裹资产清单为空或损坏')
        for item in assets:
            kind, identifier = item['kind'], safe_asset_id(item['id'])
            key = f'{kind}:{identifier}'
            if kind not in {'script', 'workflow', 'skill', 'monitor'} or key in result:
                raise AssetDeliveryError('DELIVERY_INTEGRITY_FAILED', '包裹资产身份错误或重复')
            files = item['files']
            if not isinstance(files, list) or sha256(delivery_json(files)).hexdigest() != item['content_sha256']:
                raise AssetDeliveryError('DELIVERY_INTEGRITY_FAILED', '资产摘要不匹配')
            result[key] = {}
            for file in files:
                parts = _resource_parts(file['path'])
                if file['path'] in result[key] or type(file['size']) is not int or not 0 <= file['size'] <= MAX_BUNDLE_BYTES:
                    raise AssetDeliveryError('DELIVERY_INTEGRITY_FAILED', '资源清单错误或重复')
                raw = read_bytes_beneath(self.root_dir, (*home.relative_to(self.root_dir).parts, *_BASE, 'inbox', envelope['delivery_id'],
                    'payload', kind, identifier, *parts), max_bytes=MAX_BUNDLE_BYTES, require_dir_fd=True)
                if raw is None or len(raw) != file['size'] or sha256(raw).hexdigest() != file['sha256']:
                    raise AssetDeliveryError('DELIVERY_INTEGRITY_FAILED', '资源字节已变化')
                total += len(raw)
                if total > MAX_BUNDLE_BYTES:
                    raise AssetDeliveryError('ASSET_DELIVERY_QUOTA_EXCEEDED', '投递包超过字节上限')
                result[key][file['path']] = raw
        return result

    # LLM: 已准备安装清单只引用原canonical路径，实际字节须完全一致才可补记已提交结果；不以provenance或文件存在冒充完成。
    # 函数用途: 辨识资产已原子提交但后续回执写失败的重试，防重复重命名安装。
    def _installation_matches(self, home, envelope, *, previous=False):
        from . import asset_root_lock
        from .workspace_assets import _validate_batch_path

        expected = envelope.get('resolution', {}).get('expected_files')
        if not expected:
            return False
        with asset_root_lock(home):
            for entry in expected:
                _validate_batch_path(entry['parts'])
                raw = read_bytes_beneath(self.root_dir, (*home.relative_to(self.root_dir).parts, *entry['parts']), max_bytes=MAX_BUNDLE_BYTES, require_dir_fd=True)
                key = 'previous_sha256' if previous else 'sha256'
                if key not in entry or (None if raw is None else sha256(raw).hexdigest()) != entry[key]:
                    return False
        return True

    # LLM: 原hash不变，终态重试保留updated_at并补通知；逐叶原子写，失败不得伪称无副作用，原安装清单负责辨识重试。
    # 函数用途: 结算接受或拒绝结果并同步两端状态与通知终态。
    def _resolve_delivery(self, sender, recipient, envelope, receipt, status, admissions):
        if envelope['status'] != status:
            envelope.update(status=status, updated_at=_timestamp())
        envelope.setdefault('resolution', {})['phase'] = 'resolved'
        receipt.update(status=status, updated_at=envelope['updated_at'], resolution=envelope['resolution'])
        self._put_json(sender, (*_BASE, 'outbox', envelope['delivery_id'] + '.json'), receipt, admissions[sender])
        self._put_json(recipient, (*_BASE, 'inbox', envelope['delivery_id'], 'envelope.json'), envelope, admissions[recipient])
        self._resolve_notification(recipient, envelope, admissions[recipient])
        return {'ok': True, 'delivery_id': envelope['delivery_id'], 'status': status,
            'installed_assets': envelope['resolution'].get('installed_assets', [])}

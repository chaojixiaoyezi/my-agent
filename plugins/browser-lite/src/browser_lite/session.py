# LLM: 默认无绑定调用保持单浏览器与 data/profile 行为；携卡调用按 (owner, actor, thread) 隔离临时 profile 与进程，
#   绑定凭据代次与授权 origin，拒绝跨 actor/无绑定接管，并在 close、空闲、撤销及退出时彻底清理。
# 模块用途: 管理浏览器会话的启动、按会话凭据隔离、空闲回收和关闭。

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, quote_plus

from .access import UrlGuard, http_origin
from .cdp import PageConnection
from .errors import BrowserError
from .launcher import BrowserProcess, clear_profile, find_browser, remove_profile_dir
from .page import (
    DEFAULT_READ_SELECTOR,
    click_candidate,
    click_element,
    fill_candidate,
    fill_element,
    fill_from_credential,
    inspect_credential_page,
    observation_candidates,
    open_page,
    read_elements,
)

_FATAL = {"PAGE_CRASHED", "BROWSER_DISCONNECTED"}
_DEFAULT_SCOPE = ("", "", "")
_MAX_ISOLATED_SESSIONS = 4


@dataclass
class _SessionSlot:
    scope: tuple[str, str, str]
    profile: Path | None
    ephemeral_dir: bool = False
    browser: BrowserProcess | None = None
    page: PageConnection | None = None
    last_used: float = field(default_factory=time.monotonic)
    generation: int = 0
    observed: dict[str, dict] = field(default_factory=dict)
    credential_bindings: set[tuple[str, int]] = field(default_factory=set)
    credential_origins: set[str] = field(default_factory=set)
    secret_variants: tuple[str, ...] = ()
    _raw_secrets: set[str] = field(default_factory=set, repr=False)

    def advance_generation(self) -> None:
        self.generation += 1
        self.observed = {}

    def record_secret(self, value: str) -> None:
        if not value:
            return
        self._raw_secrets.add(value)
        variants: set[str] = set()
        for item in self._raw_secrets:
            variants.update((
                item,
                json.dumps(item, ensure_ascii=True)[1:-1],
                json.dumps(item, ensure_ascii=False)[1:-1],
                quote(item, safe=""),
                quote_plus(item, safe=""),
            ))
            for encoder in (base64.b64encode, base64.urlsafe_b64encode):
                encoded = encoder(item.encode("utf-8")).decode("ascii")
                variants.update((encoded, encoded.rstrip("=")))
        self.secret_variants = tuple(sorted((v for v in variants if v), key=len, reverse=True))

    def clear_secrets(self) -> None:
        self._raw_secrets.clear()
        self.secret_variants = ()
        self.credential_bindings.clear()
        self.credential_origins.clear()


# LLM: 设置是已校验的插件设置；data_dir 为 None 表示宿主没给数据目录，此时不能启动浏览器（不回退 cwd）。
#   空闲线程是守护线程，只在拿到锁时关闭到期会话，不输出任何内容到标准输出。
# 类用途: 插件进程内的浏览器会话管理器（兼容默认单槽位与凭据按 actor/thread 隔离槽位）。
class BrowserSession:
    # LLM: 有副作用：启动空闲回收守护线程。
    # 函数用途: 初始化默认会话槽位、隔离槽位表并开始空闲检查。
    def __init__(self, settings: dict, data_dir: Path | None):
        self.settings = settings
        self.data_dir = data_dir
        self.profile = data_dir / "profile" if data_dir is not None else None
        self.lock = threading.Lock()
        self._default_slot = _SessionSlot(_DEFAULT_SCOPE, self.profile, ephemeral_dir=False)
        self._isolated_slots: OrderedDict[tuple[str, str, str], _SessionSlot] = OrderedDict()
        self._sync_default_attrs()
        self._stopped = threading.Event()
        threading.Thread(target=self._idle_loop, name="browser-lite-idle", daemon=True).start()

    def _sync_default_attrs(self) -> None:
        slot = self._default_slot
        self.browser = slot.browser
        self.page = slot.page
        self.last_used = slot.last_used
        self.generation = slot.generation
        self.observed = slot.observed

    # 函数用途: 从宿主 _meta.credential_fill 解析会话隔离键，普通无绑定调用返回默认空键。
    @staticmethod
    def _scope_from_meta(credential_meta: dict | None) -> tuple[str, str, str]:
        if not isinstance(credential_meta, dict):
            return _DEFAULT_SCOPE
        raw = credential_meta.get("session_scope")
        if not isinstance(raw, dict):
            return _DEFAULT_SCOPE
        owner_id = str(raw.get("owner_id") or "").strip()
        actor_id = str(raw.get("actor_id") or "").strip()
        thread_id = str(raw.get("thread_id") or "").strip()
        if not owner_id or not (actor_id or thread_id):
            return _DEFAULT_SCOPE
        return (owner_id, actor_id, thread_id)

    # 函数用途: 按会话隔离键取得或新建对应槽位，限制活跃隔离槽位上限以回收最久未用实例。
    def _slot_for(self, scope_key: tuple[str, str, str], *, create: bool) -> _SessionSlot | None:
        if scope_key == _DEFAULT_SCOPE:
            return self._default_slot
        slot = self._isolated_slots.get(scope_key)
        if slot is not None:
            self._isolated_slots.move_to_end(scope_key)
            return slot
        if not create:
            return None
        while len(self._isolated_slots) >= _MAX_ISOLATED_SESSIONS:
            oldest_key, _oldest = next(iter(self._isolated_slots.items()))
            self._shutdown_slot(oldest_key)
        profile = None
        if self.data_dir is not None:
            digest = hashlib.sha256(json.dumps(scope_key, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
            sessions_root = self.data_dir / "sessions"
            if sessions_root.is_symlink():
                raise BrowserError("UNSAFE_PROFILE", "插件隔离会话目录是符号链接，已拒绝启动浏览器。")
            sessions_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            profile = sessions_root / f"cred-{digest}"
        slot = _SessionSlot(scope_key, profile, ephemeral_dir=True)
        self._isolated_slots[scope_key] = slot
        return slot

    def _has_active_credential_sessions(self) -> bool:
        return any(bool(slot.credential_bindings) for slot in self._isolated_slots.values())

    # 函数用途: 宿主通知凭据已撤销/过期/换代时，立即关闭对应隔离浏览器进程并删除临时 profile。
    def revoke_scope(self, credential_meta: dict | None) -> None:
        with self.lock:
            scope_key = self._scope_from_meta(credential_meta)
            if scope_key != _DEFAULT_SCOPE:
                self._shutdown_slot(scope_key)
                self._cleanup_sessions_root()
            self._sync_default_attrs()
        raise BrowserError(
            "CREDENTIAL_REVOKED",
            "已绑定的凭据已撤销、过期或换代，浏览器受保护会话已关闭并清理。",
            side_effect="not_applied",
        )

    # LLM: open 会按需启动浏览器；其它动作要求已有页面。致命错误后关闭对应槽位再抛出。
    # 函数用途: 在锁内执行一个页面动作（含两阶段凭据填表与隔离会话校验）。
    def run(
        self,
        name: str,
        arguments: dict,
        guard: UrlGuard,
        observation: dict | None = None,
        credential_meta: dict | None = None,
    ) -> dict:
        with self.lock:
            scope_key = self._scope_from_meta(credential_meta)
            phase = str((credential_meta or {}).get("phase") or "")
            if isinstance(credential_meta, dict) and credential_meta.get("session_revoked") is True:
                if scope_key != _DEFAULT_SCOPE:
                    self._shutdown_slot(scope_key)
                    self._cleanup_sessions_root()
                raise BrowserError(
                    "CREDENTIAL_REVOKED",
                    "已绑定的凭据已撤销、过期或换代，浏览器受保护会话已关闭并清理。",
                    side_effect="not_applied",
                )
            if phase in ("inspect", "apply") and not all(scope_key):
                raise BrowserError("MISSING_CONTEXT", "凭据填表缺少已验证的宿主 owner/actor/thread 上下文。", side_effect="not_applied")
            if scope_key == _DEFAULT_SCOPE and name != "open" and self._default_slot.page is None and self._has_active_credential_sessions():
                raise BrowserError("CREDENTIAL_SESSION_ISOLATED", "当前浏览器存在受保护凭据会话，拒绝无绑定的读取或操作接管。")
            slot = self._slot_for(scope_key, create=(name == "open"))
            if slot is None:
                if self._has_active_credential_sessions():
                    raise BrowserError("CREDENTIAL_SESSION_ISOLATED", "当前浏览器会话属于其他已授权用户或会话，请先在本人会话内 open 页面。")
                raise BrowserError("NO_PAGE", "还没有打开页面（或浏览器已被空闲回收），请先用 open 打开地址。")
            slot.last_used = time.monotonic()
            try:
                if name == "open" and slot.credential_origins:
                    target_url = guard.target(arguments["url"])
                    target_origin = http_origin(target_url)
                    if target_origin is None or target_origin not in slot.credential_origins or not self._binding_matches(slot, credential_meta):
                        # 切换到新站点或无原凭据绑定时先销毁旧受保护进程与临时 profile，防止跨站残留 Cookie。
                        self._shutdown_slot_instance(slot)
                elif slot.credential_bindings and phase not in ("inspect", "apply"):
                    if not self._binding_matches(slot, credential_meta):
                        raise BrowserError("CREDENTIAL_SESSION_ISOLATED", "受保护浏览器会话要求匹配原用户、原会话与当前有效凭据代次。")
                page = self._ensure_slot_page(slot) if name == "open" else self._require_slot_page(slot)
                active_guard = guard.with_allowed_origins(slot.credential_origins) if slot.credential_origins else guard
                page.guard = active_guard.reason
                if phase == "inspect":
                    result = self._run_credential_inspect(slot, page, active_guard, arguments, observation)
                elif phase == "apply":
                    result = self._run_credential_apply(slot, page, active_guard, arguments, observation, credential_meta or {})
                elif name == "open":
                    result = open_page(page, active_guard, arguments["url"])
                    slot.advance_generation()
                    result = {**result, "browser_pid": slot.browser.pid}
                elif name == "read":
                    result = self._read_with_observation(slot, page, active_guard, arguments.get("selector"))
                else:
                    result = self._run_dom_action(slot, page, active_guard, name, arguments, observation)
                if result.get("navigated"):
                    slot.advance_generation()
                return self._scrub_slot_output(slot, result)
            except BrowserError as exc:
                if exc.code in _FATAL:
                    self._shutdown_slot(scope_key)
                raise self._scrub_browser_error(slot, exc) from None
            finally:
                if slot.page is not None:
                    slot.page.guard = None
                slot.last_used = time.monotonic()
                self._sync_default_attrs()

    @staticmethod
    def _binding_matches(slot: _SessionSlot, credential_meta: dict | None) -> bool:
        if not isinstance(credential_meta, dict):
            return False
        binding = credential_meta.get("credential_binding")
        if not isinstance(binding, dict):
            return False
        ref = binding.get("credential_ref")
        gen = binding.get("generation")
        return isinstance(ref, str) and type(gen) is int and (ref, gen) in slot.credential_bindings

    def _run_credential_inspect(
        self,
        slot: _SessionSlot,
        page: PageConnection,
        guard: UrlGuard,
        arguments: dict,
        observation: dict | None,
    ) -> dict:
        if observation is not None:
            self._resolve_candidate(slot, observation)
        elif not arguments.get("selector"):
            raise BrowserError("INVALID_ARGUMENTS", "请提供 selector，或填 read 结果里的 candidate_id。", side_effect="not_applied")
        inspected = inspect_credential_page(page, guard)
        return {**inspected, "generation": str(slot.generation)}

    def _run_credential_apply(
        self,
        slot: _SessionSlot,
        page: PageConnection,
        guard: UrlGuard,
        arguments: dict,
        observation: dict | None,
        credential_meta: dict,
    ) -> dict:
        if str(credential_meta.get("generation") or "") != str(slot.generation):
            raise BrowserError("DOCUMENT_CHANGED", "页面代次已变化，本次未填入凭据。", side_effect="not_applied")
        if str(credential_meta.get("target_id") or "") != page.target_id:
            raise BrowserError("DOCUMENT_CHANGED", "页面目标已变化，本次未填入凭据。", side_effect="not_applied")
        field_value = credential_meta.get("field_value")
        expected_origin = credential_meta.get("origin")
        document_id = credential_meta.get("document_id")
        binding = credential_meta.get("credential_binding")
        if (not isinstance(field_value, str) or not isinstance(expected_origin, str) or not expected_origin
                or not isinstance(document_id, str) or not document_id or not isinstance(binding, dict)
                or not isinstance(binding.get("credential_ref"), str) or type(binding.get("generation")) is not int):
            raise BrowserError("INVALID_CONTEXT", "宿主凭据填表参数不完整，本次未填入凭据。", side_effect="not_applied")
        if observation is not None:
            selector, index, expected = self._resolve_candidate(slot, observation)
            target: object = {"selector": selector, "index": index, "expected": expected}
        else:
            if not arguments.get("selector"):
                raise BrowserError("INVALID_ARGUMENTS", "请提供 selector，或填 read 结果里的 candidate_id。", side_effect="not_applied")
            target = arguments["selector"]
        result = fill_from_credential(
            page,
            guard,
            target,
            field_value=field_value,
            expected_origin=expected_origin,
            document_id=document_id,
        )
        slot.credential_bindings.add((binding["credential_ref"], binding["generation"]))
        slot.credential_origins.add(expected_origin)
        slot.record_secret(field_value)
        return result

    def _run_dom_action(
        self,
        slot: _SessionSlot,
        page: PageConnection,
        guard: UrlGuard,
        name: str,
        arguments: dict,
        observation: dict | None,
    ) -> dict:
        if observation is not None:
            selector, index, expected = self._resolve_candidate(slot, observation)
            if name == "click":
                return click_candidate(page, guard, selector, index, expected)
            return fill_candidate(page, guard, selector, index, expected, arguments["value"])
        if not arguments.get("selector"):
            raise BrowserError("INVALID_ARGUMENTS", "请提供 selector，或填 read 结果里的 candidate_id。")
        if name == "click":
            return click_element(page, guard, arguments["selector"])
        return fill_element(page, guard, arguments["selector"], arguments["value"])

    # 函数用途: 读取元素并附上本代次的观察候选；受保护凭据会话自动隐藏密码控件与已填秘密变体。
    def _read_with_observation(self, slot: _SessionSlot, page: PageConnection, guard: UrlGuard, selector: str | None) -> dict:
        result = read_elements(page, guard, selector, hide_passwords=bool(slot.credential_bindings))
        result = self._scrub_slot_output(slot, result)
        selector_text = selector or DEFAULT_READ_SELECTOR
        selector_hash = hashlib.sha256(selector_text.encode("utf-8")).hexdigest()[:8]
        slot.observed[selector_hash] = {"selector": selector_text, "count": int(result.get("count") or 0)}
        candidates = observation_candidates(result.get("items") or [], selector_hash)
        if candidates:
            result["my_agent_observation"] = {
                "schema": "plugin_observation.v1",
                "target": {"ref": "page", "generation": str(slot.generation)},
                "candidates": self._scrub_slot_output(slot, candidates),
            }
        return result

    # 函数用途: 把候选 key 解析回“同一选择器 + 序号 + 观察时总数”。
    def _resolve_candidate(self, slot: _SessionSlot, observation: dict) -> tuple[str, int, int]:
        target = observation.get("target") if isinstance(observation, dict) else None
        if (not isinstance(target, dict) or target.get("ref") != "page"
                or str(target.get("generation")) != str(slot.generation)):
            raise BrowserError(
                "OBSERVATION_STALE",
                "页面已经变化，观察候选已过期，请重新 read 后再操作。",
                my_agent_observation_error={"code": "stale"},
                side_effect="not_applied",
            )
        key = str(observation.get("key") or "")
        selector_hash, _, index = key.partition(".")
        entry = slot.observed.get(selector_hash)
        if entry is None or not index.isdigit():
            raise BrowserError(
                "OBSERVATION_NOT_FOUND",
                "候选不属于本页当前代次的任何一次 read，请重新 read。",
                my_agent_observation_error={"code": "not_found"},
                side_effect="not_applied",
            )
        return entry["selector"], int(index), int(entry["count"])

    @staticmethod
    def _scrub_slot_output(slot: _SessionSlot, value):
        variants = slot.secret_variants
        if not variants:
            return value
        return _scrub_value(value, variants)

    @classmethod
    def _scrub_browser_error(cls, slot: _SessionSlot, exc: BrowserError) -> BrowserError:
        if not slot.secret_variants:
            return exc
        message = _scrub_value(str(exc), slot.secret_variants)
        extra = {key: _scrub_value(val, slot.secret_variants) for key, val in exc.extra.items()}
        return BrowserError(exc.code, message, **extra)

    # LLM: 有副作用：按会话隔离键关闭对应浏览器并清理缓存（无隔离键时关闭默认浏览器并清空默认 profile）。
    # 函数用途: 响应 close 工具。
    def close_browser(self, credential_meta: dict | None = None) -> dict:
        with self.lock:
            scope_key = self._scope_from_meta(credential_meta)
            if scope_key != _DEFAULT_SCOPE:
                slot = self._isolated_slots.get(scope_key)
                running = slot is not None and slot.browser is not None and slot.browser.alive()
                self._shutdown_slot(scope_key)
            else:
                if self._default_slot.browser is None and self._has_active_credential_sessions():
                    raise BrowserError("CREDENTIAL_SESSION_ISOLATED", "受保护凭据浏览器会话仅能由原授权会话关闭。")
                running = self._default_slot.browser is not None and self._default_slot.browser.alive()
                self._shutdown_slot(_DEFAULT_SCOPE)
                if self.profile is not None:
                    clear_profile(self.profile)
            self._sync_default_attrs()
        return {
            "closed": running,
            "message": "浏览器已关闭，profile 缓存已清理。" if running else "浏览器未在运行，profile 缓存已清理。",
        }

    # LLM: 进程退出路径调用；停止空闲线程并关闭全部默认与隔离浏览器槽位。
    # 函数用途: 插件退出时关闭全部浏览器并移除隔离临时 profile。
    def shutdown(self) -> None:
        self._stopped.set()
        if self.lock.acquire(timeout=10):
            try:
                for key in list(self._isolated_slots):
                    self._shutdown_slot(key)
                self._shutdown_slot(_DEFAULT_SCOPE)
                self._cleanup_sessions_root()
                self._sync_default_attrs()
            finally:
                self.lock.release()

    def _cleanup_sessions_root(self) -> None:
        if self.data_dir is None:
            return
        sessions_root = self.data_dir / "sessions"
        if sessions_root.is_symlink() or not sessions_root.is_dir():
            return
        if not any(sessions_root.iterdir()):
            try:
                sessions_root.rmdir()
            except OSError:
                pass

    # 函数用途: 取得槽位可用的页面连接，必要时启动浏览器。
    def _ensure_slot_page(self, slot: _SessionSlot) -> PageConnection:
        if slot.page is not None and slot.browser is not None and slot.browser.alive():
            return slot.page
        self._shutdown_slot_instance(slot)
        if slot.profile is None:
            raise BrowserError("MISSING_DATA_DIR", "宿主未提供插件数据目录，无法创建专属浏览器 profile。")
        timeout = float(self.settings["command_timeout_seconds"])
        browser = BrowserProcess(find_browser(self.settings["chrome_path"]), slot.profile, timeout)
        browser.start()
        try:
            slot.page = PageConnection.open(browser.port, browser.page_target(), timeout)
        except BaseException:
            browser.stop()
            if slot.ephemeral_dir and slot.profile is not None:
                remove_profile_dir(slot.profile)
            raise
        slot.browser = browser
        return slot.page

    # 函数用途: 要求槽位已有打开的页面。
    def _require_slot_page(self, slot: _SessionSlot) -> PageConnection:
        if slot.page is None or slot.browser is None or not slot.browser.alive():
            self._shutdown_slot_instance(slot)
            raise BrowserError("NO_PAGE", "还没有打开页面（或浏览器已被空闲回收），请先用 open 打开地址。")
        return slot.page

    def _shutdown_slot(self, scope_key: tuple[str, str, str]) -> None:
        if scope_key == _DEFAULT_SCOPE:
            self._shutdown_slot_instance(self._default_slot)
            return
        slot = self._isolated_slots.pop(scope_key, None)
        if slot is not None:
            self._shutdown_slot_instance(slot)
        self._cleanup_sessions_root()

    def _shutdown_slot_instance(self, slot: _SessionSlot) -> None:
        page, browser = slot.page, slot.browser
        slot.page, slot.browser = None, None
        slot.observed = {}
        slot.clear_secrets()
        if page is not None:
            page.close()
        if browser is not None:
            browser.stop()
        if slot.ephemeral_dir and slot.profile is not None:
            remove_profile_dir(slot.profile)

    # 函数用途: 空闲超时后自动关闭到期的默认或隔离浏览器槽位。
    def _idle_loop(self) -> None:
        idle = float(self.settings["idle_close_seconds"])
        while not self._stopped.wait(1.0):
            if not self.lock.acquire(blocking=False):
                continue
            try:
                now = time.monotonic()
                if self._default_slot.browser is not None and now - self._default_slot.last_used >= idle:
                    self._shutdown_slot(_DEFAULT_SCOPE)
                for key, slot in list(self._isolated_slots.items()):
                    if now - slot.last_used >= idle:
                        self._shutdown_slot(key)
                self._sync_default_attrs()
            finally:
                self.lock.release()


def _scrub_value(value, variants: tuple[str, ...]):
    if isinstance(value, str):
        for item in variants:
            value = value.replace(item, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {_scrub_value(k, variants): _scrub_value(v, variants) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item, variants) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, variants) for item in value)
    return value

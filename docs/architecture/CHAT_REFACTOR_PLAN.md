# Chat Refactor Plan
# Chat 模块重构计划：从 989 行上帝函数到模块化架构

LLM: Split chat.py by interaction responsibility without changing CLI behavior.
给人看的解释：`chat.py` 现在 989 行，是全仓最大的文件。它同时承担交互循环、TUI 渲染、slash 命令处理、gateway 客户端、会话状态管理和 fallback 模式。需要拆成职责单一的小模块。

---

## 1. Current State Analysis / 现状分析

### 1.1 File Structure / 文件结构

`cli/chat.py` 当前 989 行，包含以下函数和内部类：

```
chat.py (989行)
├── _cprint()                          # 80行   彩色打印工具
├── cmd_chat()                         # 90行   主入口，会话初始化
│   └── _build_history_context()       # 135行  历史上下文构建
├── _run_tui()                         # 187行  prompt_toolkit TUI 模式
│   ├── _get_status_text()             # 223行  状态栏文本
│   ├── _print_banner()                # 252行  启动 banner
│   ├── _render_user_entry()           # 266行  用户输入渲染
│   ├── _render_assistant_response()   # 276行  助手响应渲染
│   ├── _handle_expand_command()       # 284行  展开命令处理
│   ├── _request_exit()                # 307行  退出请求
│   ├── handle_command()               # 315行  斜杠命令处理
│   ├── _set_thinking_line()           # 346行  思考状态行
│   ├── _emit_stream_line()            # 358行  流式输出行
│   ├── _flush_stream_buf()            # 362行  流式缓冲刷新
│   ├── _append_stream_text()          # 369行  流式文本追加
│   ├── worker()                       # 380行  后台工作线程
│   │   └── _on_spinner_update()       # 400行  spinner 更新回调
│   │   └── _begin_stream()            # 411行  流式开始
│   │   └── _on_stream_chunk()         # 419行  流式 chunk 回调
│   ├── enqueue_job()                  # 544行  入队任务
│   ├── key binding handlers           # 582-605行  键绑定
│   └── _refresh_loop()                # 636行  刷新循环
└── _run_fallback()                    # 673行  非 TUI 降级模式
    ├── render_assistant_response()    # 698行  响应渲染
    ├── handle_expand_command()        # 708行  展开命令
    ├── redraw_fallback_prompt()       # 730行  重绘提示符
    └── worker()                       # 738行  后台工作线程
        └── _on_chat_chunk()           # 837行  chunk 回调
```

### 1.2 Key Problems / 关键问题

**问题 1: 巨型嵌套函数**
`_run_tui()` 函数体超过 450 行，内部定义了 20+ 个嵌套函数。这些嵌套函数通过闭包捕获外部状态（`is_running`, `pending_jobs`, `conversation_history` 等），导致状态管理分散且难以测试。

**问题 2: TUI 和 Fallback 代码重复**
`_run_tui()` 和 `_run_fallback()` 包含大量重复逻辑：
- 后台 worker 线程逻辑（模型调用、结果处理）
- 斜杠命令处理（`/exit`, `/clear`, `/history` 等）
- 响应渲染逻辑
- 流式输出处理

**问题 3: 状态管理混乱**
状态分散在多个变量中：
- `is_running` — 是否正在执行
- `pending_jobs` — 待处理任务数
- `shutting_down` — 是否正在关闭
- `conversation_history` — 对话历史
- `current_session_id` — 当前会话 ID
- `running_prompt` — 当前运行的 prompt
- `running_started_at` — 开始时间
- `last_token_estimate` — token 估计

这些变量通过闭包在多个嵌套函数间共享，没有统一的状态管理。

**问题 4: 渲染与业务逻辑混合**
渲染逻辑（颜色、格式化、进度条）与业务逻辑（命令处理、会话管理、gateway 通信）交织在一起。

**问题 5: 嵌套过深**
多处嵌套 8-9 层，超过 4 层上限。例如 `worker()` 内部定义了 `_on_spinner_update()`, `_begin_stream()`, `_on_stream_chunk()` 等嵌套函数，每个又有自己的嵌套。

### 1.3 Already Extracted / 已提取部分

已提取到 `cli/chat_parts/` 的模块：

| 模块 | 职责 | 行数 | 状态 |
|---|---|---|---|
| `chat_parts/history.py` | 会话历史管理（MAX_HISTORY_TURNS, append, build） | 48 | 完成 |
| `chat_parts/rendering.py` | 终端渲染（颜色、进度条、banner、折叠） | 64 | 完成 |
| `chat_parts/slash_commands.py` | 公共斜杠命令处理 | 89 | 完成 |

---

## 2. Target Decomposition / 目标分解

### 2.1 Target Module Structure / 目标模块结构

```
cli/
├── chat.py                    # [目标: <100行] 公共入口，委托给 chat_parts/
│   └── cmd_chat()             #   唯一公开函数
│
├── chat_parts/                # [已存在 + 待扩展] chat 子模块
│   ├── __init__.py
│   ├── history.py             # [已完成] 会话历史管理
│   ├── rendering.py           # [已完成] 终端渲染
│   ├── slash_commands.py      # [已完成] 公共斜杠命令
│   ├── session_state.py       # [待创建] 会话状态管理
│   ├── input_loop.py          # [待创建] prompt_toolkit 循环和中断处理
│   ├── gateway_client.py      # [待创建] gateway 通信客户端
│   ├── tui_runner.py          # [待创建] TUI 模式主循环
│   └── fallback_runner.py     # [待创建] fallback 模式主循环
```

### 2.2 Module Responsibilities / 模块职责

#### `chat_parts/session_state.py` — 会话状态管理

**职责**: 集中管理所有会话状态变量，提供线程安全的状态访问

**当前来源**: `cmd_chat()` 中的状态变量（`is_running`, `pending_jobs`, `shutting_down` 等）

```python
@dataclass
class SessionState:
    """会话状态，替代 cmd_chat() 中散落的状态变量。"""

    session_id: str = ""
    is_running: bool = False
    pending_jobs: int = 0
    shutting_down: bool = False
    running_prompt: str = ""
    running_started_at: float = 0.0
    last_token_estimate: int = 0
    conversation_history: list[tuple[str, str]] = field(default_factory=list)
    history_lock: threading.Lock = field(default_factory=threading.Lock)
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    runtime_inject: list[str] = field(default_factory=list)
    prompt_files: list[str] = field(default_factory=list)

    def mark_running(self, prompt: str) -> None:
        """标记为运行中，线程安全。"""
        with self.state_lock:
            self.is_running = True
            self.running_prompt = prompt
            self.running_started_at = time.time()
            self.pending_jobs += 1

    def mark_idle(self) -> None:
        """标记为空闲，线程安全。"""
        with self.state_lock:
            self.is_running = False
            self.running_prompt = ""
            self.pending_jobs = max(0, self.pending_jobs - 1)

    def request_exit(self) -> None:
        """请求退出。"""
        self.shutting_down = True

    def build_history_context(self, max_turns: int = 10) -> str:
        """构建历史上下文。"""
        return build_history_context(
            self.conversation_history,
            self.history_lock,
            max_turns=max_turns,
        )
```

**依赖**: `chat_parts/history.py`（复用 `build_history_context`）

#### `chat_parts/input_loop.py` — 输入循环

**职责**: prompt_toolkit 的 PromptSession 初始化、键绑定、输入读取

**当前来源**: `_run_tui()` 中的 `PromptSession` 初始化和键绑定代码

```python
def create_prompt_session() -> PromptSession | None:
    """创建 prompt_toolkit 会话，如果不可用返回 None。"""
    if PromptSession is None:
        return None
    return PromptSession(
        history=FileHistory(".chat_history"),
        auto_suggest=AutoSuggestFromHistory(),
    )

def create_keybindings(state: SessionState) -> KeyBindings:
    """创建键绑定。"""
    bindings = KeyBindings()

    @bindings.add("c-c")
    def _(event):
        """Ctrl-C 中断。"""
        if state.is_running:
            # 中断当前任务
            pass
        else:
            state.request_exit()

    @bindings.add("c-d")
    def _(event):
        """Ctrl-D 退出。"""
        state.request_exit()

    return bindings
```

**依赖**: `prompt_toolkit`（可选依赖）

#### `chat_parts/gateway_client.py` — Gateway 客户端

**职责**: gateway 模式的通信封装

**当前来源**: `_run_tui()` 和 `_run_fallback()` 中的 gateway 调用代码

```python
class GatewayChatClient:
    """Gateway 模式的聊天客户端。"""

    def __init__(self, paths: dict):
        self.paths = paths

    def is_running(self) -> bool:
        """检查 gateway 是否运行中。"""
        return gateway_running(self.paths)

    def submit_and_wait(self, prompt: str, **kwargs) -> str:
        """提交 prompt 并等待响应。"""
        submit_gateway_ask(prompt, self.paths, **kwargs)
        return wait_for_gateway_response(self.paths, **kwargs)

    def check_status(self) -> dict:
        """检查 gateway 状态。"""
        return render_gateway_status(self.paths)
```

**依赖**: `agent.gateway`（gateway 工具函数）

#### `chat_parts/tui_runner.py` — TUI 模式主循环

**职责**: prompt_toolkit 增强的交互循环

**当前来源**: `_run_tui()` 函数（450+ 行）

```python
def run_tui(
    agent,
    args,
    state: SessionState,
    use_gateway: bool,
    paths: dict,
) -> int:
    """TUI 模式主循环。"""
    session = create_prompt_session()
    bindings = create_keybindings(state)
    renderer = ResponseRenderer(use_color=True)
    gateway = GatewayChatClient(paths) if use_gateway else None

    # 启动后台 worker
    worker = ChatWorker(agent=agent, state=state, renderer=renderer, gateway=gateway)
    worker.start()

    # 打印 banner
    renderer.print_banner()

    # 主循环
    while not state.shutting_down:
        try:
            user_input = session.prompt(CHAT_PROMPT, key_bindings=bindings)
        except (EOFError, KeyboardInterrupt):
            state.request_exit()
            break

        if not user_input.strip():
            continue

        if handle_common_slash_command(user_input, state, renderer):
            continue

        worker.enqueue(user_input)

    return 0
```

**依赖**: `chat_parts/session_state.py`, `chat_parts/input_loop.py`, `chat_parts/gateway_client.py`, `chat_parts/rendering.py`, `chat_parts/slash_commands.py`

#### `chat_parts/fallback_runner.py` — 降级模式主循环

**职责**: 无 prompt_toolkit 时的降级交互

**当前来源**: `_run_fallback()` 函数（317 行）

```python
def run_fallback(
    agent,
    args,
    state: SessionState,
    use_gateway: bool,
    paths: dict,
) -> int:
    """降级模式主循环。"""
    renderer = ResponseRenderer(use_color=True)
    gateway = GatewayChatClient(paths) if use_gateway else None

    # 启动后台 worker
    worker = ChatWorker(agent=agent, state=state, renderer=renderer, gateway=gateway)
    worker.start()

    # 主循环
    while not state.shutting_down:
        try:
            user_input = input(FALLBACK_CHAT_PROMPT)
        except (EOFError, KeyboardInterrupt):
            state.request_exit()
            break

        if not user_input.strip():
            continue

        if handle_common_slash_command(user_input, state, renderer):
            continue

        worker.enqueue(user_input)

    return 0
```

**依赖**: `chat_parts/session_state.py`, `chat_parts/rendering.py`, `chat_parts/slash_commands.py`

### 2.3 Shared Worker / 共享 Worker

TUI 和 Fallback 模式共享同一个后台工作线程，消除代码重复：

```python
class ChatWorker:
    """后台聊天工作线程，TUI 和 Fallback 共用。"""

    def __init__(self, agent, state: SessionState, renderer, gateway=None):
        self.agent = agent
        self.state = state
        self.renderer = renderer
        self.gateway = gateway
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动工作线程。"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def enqueue(self, prompt: str) -> None:
        """入队任务。"""
        self._jobs.put(ChatJob(prompt=prompt))

    def _run(self) -> None:
        """工作线程主循环。"""
        while not self.state.shutting_down:
            try:
                job = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            self._execute_job(job)

    def _execute_job(self, job: ChatJob) -> None:
        """执行单个任务。"""
        self.state.mark_running(job.prompt)
        try:
            if self.gateway:
                result = self.gateway.submit_and_wait(job.prompt)
            else:
                result = self.agent.run(job.prompt)
            self.renderer.render_assistant_response(result)
        except Exception as e:
            self.renderer.render_error(str(e))
        finally:
            self.state.mark_idle()
```

---

## 3. Migration Steps / 迁移步骤

### Phase 1: Extract Pure Functions（低风险）

#### Step 1: 提取 SessionState
- [ ] 创建 `cli/chat_parts/session_state.py`
- [ ] 将 `cmd_chat()` 中的状态变量（`is_running`, `pending_jobs`, `shutting_down`, `conversation_history`, `history_lock`, `state_lock`）移入 `SessionState` dataclass
- [ ] 更新 `cmd_chat()` 创建 `SessionState` 实例并传递给 `_run_tui()` 和 `_run_fallback()`
- [ ] 测试: 会话创建、状态变更、退出请求
- **风险**: 低 — 只是数据结构提取，不改变逻辑

#### Step 2: 提取 GatewayChatClient
- [ ] 创建 `cli/chat_parts/gateway_client.py`
- [ ] 将 `_run_tui()` 和 `_run_fallback()` 中的 gateway 调用代码封装
- [ ] 测试: gateway 模式行为不变
- **风险**: 低 — 只是封装，不改变调用方式

### Phase 2: Extract Runners（中风险）

#### Step 3: 提取 ChatWorker
- [ ] 创建 `cli/chat_parts/worker.py`
- [ ] 将 `_run_tui()` 和 `_run_fallback()` 中的 worker 逻辑提取为共享类
- [ ] 消除 TUI 和 Fallback 的代码重复
- [ ] 测试: 后台任务执行行为不变
- **风险**: 中 — 涉及线程和状态管理

#### Step 4: 提取 InputLoop
- [ ] 创建 `cli/chat_parts/input_loop.py`
- [ ] 将 prompt_toolkit 初始化和键绑定代码提取
- [ ] 测试: 键绑定行为不变
- **风险**: 中 — prompt_toolkit 生命周期管理

#### Step 5: 提取 TuiRunner
- [ ] 创建 `cli/chat_parts/tui_runner.py`
- [ ] 将 `_run_tui()` 的主循环逻辑移入
- [ ] 使用 SessionState、ChatWorker、InputLoop 等组件
- [ ] 测试: TUI 完整交互流程不变
- **风险**: 中 — 涉及整个 TUI 模式

#### Step 6: 提取 FallbackRunner
- [ ] 创建 `cli/chat_parts/fallback_runner.py`
- [ ] 将 `_run_fallback()` 的主循环逻辑移入
- [ ] 使用 SessionState、ChatWorker 等组件
- [ ] 测试: Fallback 完整交互流程不变
- **风险**: 中 — 涉及整个 Fallback 模式

### Phase 3: Thin Entry Point（低风险）

#### Step 7: 简化 chat.py 入口
- [ ] `cli/chat.py` 简化为 <100 行的入口
- [ ] `cmd_chat()` 只负责：参数解析、创建 SessionState、选择 TUI/Fallback、委托执行
- [ ] 测试: `cmd_chat()` 行为不变
- **风险**: 低 — 只是委托，不改变逻辑

#### Step 8: 降低 Guardrail 限制
- [ ] 更新 `scripts/check_code_size.py`，将 `chat.py` 的 HARD 限制降低到 100 行
- [ ] 运行全量测试确认
- **风险**: 低 — 只是配置变更

---

## 4. Compatibility Rules / 兼容性规则

### 4.1 Behavioral Invariants / 行为不变量

迁移过程中必须保持的行为：

| 行为 | 说明 | 验证方式 |
|---|---|---|
| 启动 banner | 启动时显示的 banner 文本不变 | 快照测试 |
| 提示符 | `CHAT_PROMPT` 和 `FALLBACK_CHAT_PROMPT` 不变 | 快照测试 |
| 斜杠命令 | 所有 `/xxx` 命令的行为不变 | 集成测试 |
| 快捷键 | Enter, Ctrl-C, Ctrl-D 等行为不变 | 手动测试 |
| 颜色方案 | 所有颜色代码不变 | 快照测试 |
| 响应格式 | 助手响应的格式化方式不变 | 快照测试 |
| 流式输出 | 流式 chunk 的渲染方式不变 | 快照测试 |
| 错误处理 | 错误消息的格式和内容不变 | 快照测试 |
| 会话管理 | 会话创建、恢复、切换的行为不变 | 集成测试 |
| Gateway 模式 | gateway 通信的行为不变 | 集成测试 |

### 4.2 Prompt Text Rules / 提示文本规则

- 不改变 prompt 文本，除非有测试覆盖或明确要求
- 不改变 `.chat_history` 路径行为
- 交互行为在有/无 `prompt_toolkit` 时都必须正常工作

### 4.3 Output Format Rules / 输出格式规则

- 所有 slash 命令的输出格式不变
- gateway 客户端行为不变
- 错误消息格式不变

---

## 5. Risk Assessment / 风险评估

| 阶段 | 风险 | 缓解措施 |
|---|---|---|
| Phase 1: SessionState | 低 | 只是数据结构提取，不改变逻辑 |
| Phase 1: GatewayClient | 低 | 只是封装，不改变调用方式 |
| Phase 2: ChatWorker | 中 | 涉及线程和状态管理，需仔细测试 |
| Phase 2: InputLoop | 中 | prompt_toolkit 生命周期管理复杂 |
| Phase 2: TuiRunner | 中 | 涉及整个 TUI 模式 |
| Phase 2: FallbackRunner | 中 | 涉及整个 Fallback 模式 |
| Phase 3: 简化入口 | 低 | 只是委托，不改变逻辑 |

### 5.1 prompt_toolkit 兼容性 / prompt_toolkit 兼容性

**风险**: prompt_toolkit 的 `PromptSession`, `KeyBindings`, `Application` 等对象的初始化顺序和生命周期管理复杂。

**缓解**: 将 prompt_toolkit 相关代码集中在 `input_loop.py` 中，其他模块不直接依赖 prompt_toolkit。

### 5.2 Threading Safety / 线程安全

**风险**: 当前通过闭包共享状态，迁移后通过 `SessionState` 共享，锁的使用方式可能变化。

**缓解**: `SessionState` 内置锁机制，所有状态变更通过方法调用，不直接访问属性。

### 5.3 Import Cycle / 循环导入

**风险**: `chat_parts/` 子模块可能产生循环导入。

**缓解**: 依赖方向单向：`chat.py` -> `tui_runner.py` / `fallback_runner.py` -> `worker.py` -> `session_state.py`。不允许反向导入。

### 5.4 Performance / 性能

**风险**: 额外的对象创建和方法调用可能影响启动速度。

**缓解**: 延迟初始化（lazy init），只在需要时创建对象。交互延迟主要由模型调用决定，对象创建开销可忽略。

---

## 6. Testing Strategy / 测试策略

### 6.1 Snapshot Tests / 快照测试

对渲染输出进行快照测试，确保迁移前后输出完全一致：

```python
def test_startup_banner_unchanged(capsys):
    """启动 banner 输出不变。"""
    renderer = ResponseRenderer(use_color=False)
    renderer.print_banner()
    output = capsys.readouterr().out
    assert output == EXPECTED_BANNER  # 快照值

def test_assistant_response_format(capsys):
    """助手响应格式不变。"""
    renderer = ResponseRenderer(use_color=False)
    renderer.render_assistant_response("Hello, world!")
    output = capsys.readouterr().out
    assert output == EXPECTED_RESPONSE_FORMAT  # 快照值
```

### 6.2 Integration Tests / 集成测试

测试完整的交互流程：

```python
def test_slash_exit_behavior():
    """斜杠退出命令行为不变。"""
    state = SessionState()
    handle_common_slash_command("/exit", state, ResponseRenderer())
    assert state.shutting_down is True

def test_conversation_history_roundtrip():
    """对话历史往返不变。"""
    state = SessionState()
    state.conversation_history.append(("user", "hello"))
    state.conversation_history.append(("assistant", "hi"))
    context = state.build_history_context()
    assert "hello" in context
    assert "hi" in context
```

### 6.3 Thread Safety Tests / 线程安全测试

```python
def test_concurrent_state_updates():
    """并发状态更新安全。"""
    state = SessionState()
    threads = []
    for i in range(100):
        t = threading.Thread(target=state.mark_running, args=(f"prompt-{i}",))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    assert state.pending_jobs == 100
```

---

## 7. Verification Commands / 验证命令

每步迁移后必须运行：

```bash
# 单元测试
python -m pytest agent_py_agent/tests/test_chat_parts.py -q

# CLI 测试
python -m pytest agent_py_agent/tests/test_cli_parser.py -q

# 编译检查
python -m compileall -q agent_py_agent

# 代码大小检查
python scripts/check_code_size.py --mode warn

# star import 检查
python -m pytest agent_py_agent/tests/test_no_star_imports.py -q
```

---

## 8. Rollback Strategy / 回滚策略

如果迁移导致问题，可以通过以下方式回滚：

1. 将 `chat.py` 恢复到 git 中的上一个版本
2. 所有 `chat_parts/` 新增模块保留但不使用
3. `cmd_chat()` 恢复为直接实现

由于 `cmd_chat()` 是唯一的公开入口，回滚只需要恢复这一个文件。所有 `chat_parts/` 模块都是内部实现，不影响外部 API。

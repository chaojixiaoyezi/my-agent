# log_ops 自适应演进技术方案(待审)

> 目标:从"代码写死的 15 条规则 + 按行切割"升级为"LLM 先学习每个源 → 现场决定切割/初筛/研判方向 → 固化成方案让 daemon 长期执行 → 按需派发研判(几个月持续)"。
> 状态:**方案待审,未动代码**。现有 log_ops(两层架构 + 不丢 + 常驻)全部保留,本方案是**在地基上接三根线**,默认行为向后兼容。

---

## 0. 范式转变

| 维度 | 现状(规则驱动) | 目标(方案驱动) |
|---|---|---|
| 规则来源 | 代码常量 `triage.py:_RULE_DEFS`(全局 15 条) | LLM 探查后生成,**per-源**,存进配置 |
| 切割方式 | 写死按 `\n` 切行(`collector.py`) | LLM 现场定:行/JSON/多行合并/分隔符 |
| 接入流程 | `log_monitor_start` 直接套死规则开跑 | 先**采样→LLM 分析→定方案→写回**,再开跑 |
| 研判 | 单主代理 poll 循环 | 攒候选→**按需唤醒**派发研判子代理 |
| 适应性 | 格式一变规则就失效,无人知 | 命中率/解析率漂移检测→触发重新探查 |

核心:**把 LLM 学到的策略从"硬编码"外置成"数据(SourceProfile)"**。两层分离的地基天生支持这个——确定性层本来就是"执行策略",只是现在策略写死了。

---

## 1. 核心数据结构:SourceProfile

每个源一份,LLM 探查产出,持久化进配置,daemon 读它执行。

```jsonc
{
  "source_id": "api-bc4b68-9001",
  "locator": "http://127.0.0.1:9001/poll",
  "kind": "api",
  "profile": {
    "version": 1,
    "created_by": "llm-onboarding",      // 谁定的:llm-onboarding / human / default
    "created_at": 1781650000.0,
    "format_note": "JSON-per-line 的登录服务审计日志",   // LLM 人读判定

    "splitter": {                         // ── 块②:怎么切成"逻辑记录"
      "type": "json_lines",               // line | json_lines | multiline_start | delimiter
      "params": {}
    },

    "fields": {                           // 可选:从记录抽结构化字段(给规则/研判用)
      "ts":    {"from": "json", "path": "timestamp"},
      "level": {"from": "json", "path": "level"},
      "msg":   {"from": "json", "path": "message"},
      "src_ip":{"from": "regex", "pattern": "from ([0-9.]+)"}
    },

    "rules": [                            // ── 块③a:LLM 生成的 per-源初筛规则
      {"name": "auth_fail_burst", "severity": "high",
       "field": "msg", "pattern": "failed login"},
      {"name": "off_hours_admin", "severity": "medium",
       "field": "msg", "pattern": "admin.*login", "note": "结合 ts 非工作时间"}
    ],

    "triage_hint": "登录服务审计日志。重点:短时大量失败登录(基线<5/min)、非工作时间 admin 登录、异常源 IP。正常:每分钟约 200 条成功登录。研判时优先看 src_ip 聚集 + 时间窗。",

    "baseline": {                         // 可选:正常基线,便于异常/突变检测
      "normal_rate_per_min": 200,
      "alert_rate_per_min": 5,
      "known_good_ips": ["10.0.0.0/8"]
    }
  }
}
```

设计要点:
- **向后兼容**:源没有 profile → 用默认 `{splitter:line, rules:DEFAULT_RULES}`,行为 == 现状。
- **可迭代**:profile 不是一次定死。LLM 可以先定 v1,跑一会回看候选质量,再升 v2(`version` 自增)。
- **存哪**:扩展 `config.json` 的每个 source 项,挂一个 `profile` 字段(原子写,已有机制)。

---

## 2. 块①:探查器(Source Profiler)—— "学习→定方案"阶段

**目的**:正式监控前,让 LLM 看懂每个源。

**工作流**(LLM 主导,每个源接入时一次性):
```
1. log_source_sample(source)  → 采样真实数据(不污染正式监控)
2. LLM 分析样本 → 判定:格式? 字段? 怎么切? 什么算异常? 研判方向?
3. log_profile_set(source, profile)  → 写回 SourceProfile
4.(可选)log_monitor_start 试跑 → log_alert_poll 回看候选质量 → 不满意回到 2 调 profile
```

**新工具**(`tools.py`):
- `log_source_sample`:对单个源采样头部+尾部 N 行/N 字节(API 拉一批),返回真实样本。**只读,不推进 offset、不归档**(因为方案还没定,不能用错误的切割污染正式 archive)。
- `log_profile_set` / `log_profile_get`:读写某源的 SourceProfile。

**新代码**:`profiler.py`(采样逻辑,复用 collector 的读取但只读不推进)。

**改动**:`store.py` 加 profile 读写;`tools.py` 加 3 个工具。

**难点**:采样要有代表性(头部可能是启动噪声,尾部才是稳态)→ 头尾都采 + 可指定采样量。

---

## 3. 块②:可配置切割器(Record Splitter)—— 最硬的一块

**目的**:把"按行写死"升级为"按 profile 切逻辑记录"。**这块和我们刚修的 folder bug 同源**(那个残行/静默兜底逻辑),正好统一做扎实。

### 3.1 统一抽象

```python
class RecordSplitter(Protocol):
    def split(self, text: str, *, flush_trailing: bool) -> tuple[list[str], int]:
        """把读到的 text 切成完整记录列表 + 已消费字节数。
        - 返回的 consumed 决定 offset 推进多少;未消费的尾巴留到下次(增量不丢的关键)。
        - flush_trailing=True(文件 mtime 静默=写完了)时,把末尾未闭合的记录也算完整 flush 出来。
          ← 这正是现在 collect_file 的"残行保护 + 静默兜底",抽象成所有切割器的通用语义。
        """
```

**关键洞察**:line 的"残行"和 multiline 的"未闭合记录"是**同一个概念——边界未定的尾巴**。统一成 `flush_trailing` 语义后:
- 增量采集照旧:offset 只推进到 `consumed`(最后一条**已确认完整**的记录边界)
- 状态文件不变:还是单个 `offset`,splitter 是**无状态纯函数**(边界跨 offset 的未完成记录靠"不推进 offset"留到下次,而非在状态里存缓冲)
- 不丢机制照旧:落档顺序、断点续采、mtime 静默兜底全部复用

### 3.2 四种切割器

| type | 适用 | consumed 规则 |
|---|---|---|
| `line`(默认) | 行式日志(现状) | 到最后一个 `\n`;flush 时到 EOF(=刚修的兜底) |
| `json_lines` | 每行一个 JSON 对象 | 按行,坏行跳过但占位 |
| `multiline_start` | Java 堆栈/时间戳开头的多行事件 | 到**最后一个** start_pattern 之前(最后一条可能还在长,不推进);flush 时到 EOF |
| `delimiter` | 空行/`---`/自定义分隔的块 | 到最后一个完整分隔符;flush 时到 EOF |

`multiline_start` 例:`{"start_pattern": "^\\d{4}-\\d{2}-\\d{2}T"}` → 时间戳开头的行是新记录,后续非时间戳行(堆栈)合并进来。

### 3.3 改动

- 新 `splitter.py`:抽象 + 4 实现。**把现在 collect_file 的按行逻辑原样抽成 `LineSplitter`(含静默兜底),保证默认行为零变化**。
- `collector.py`:collect_file 改成"读字节 → `splitter.split(text, flush_trailing=file_idle)` → 记录",offset 按 consumed 推进。collect_folder 子文件同理继承。
- `triage.py`:规则对"记录"(可能多行)匹配,而非"行";支持先抽 `field` 再匹配。
- `daemon.py`:`_collect_one_source` 按该源 profile 选 splitter + rules(没 profile 走默认)。

### 3.4 难点(最高风险)

**切割器 × 增量 × 不丢的正确性**。multiline 边界、断点续采、mtime 兜底必须严丝合缝。缓解:**复用我们刚建的 no_loss 测试范式**——每种切割器都做"断点重启续采不重不丢 + 脏数据(无换行尾/半条记录)"测试。这次 folder bug 的教训(单测数据太规整掩盖 bug)直接用上:测试要造多行截断、坏 JSON、跨 offset 边界的记录。

---

## 4. 块③:数据驱动规则 + 研判派发

### 4.1 数据驱动规则(3a)

- `triage_line(...rules=)` **已接受外部规则**(接口现成),改成 daemon 按 per-源 profile.rules 跑。
- 规则格式复用 `TriageRule(name/severity/pattern)` + 可选 `field`(先抽字段再匹配)。
- 保留 `DEFAULT_RULES` 作兜底(没定制时)。
- **安全(必做)**:LLM 生成的正则要防 ReDoS——加正则编译校验 + **匹配超时保护** + "命中率监控"(某规则命中率 >X% → 警告太宽,等于没初筛)。

### 4.2 研判派发调度(3b)—— 几个月持续的关键

两种模式,需你拍板:

**模式 A:主代理低频值班派发**
```
主代理 wait(值班 gate 支持) → poll 看候选量 → 攒够/够严重
  → create_subagents 派研判子代理(每子代理带该源 profile.triage_hint 研判一批)
  → 收结论 → 汇总 → 确认的真威胁渐进上报用户
```
- 优点:简单,复用现有 create_subagents(native 已验证) + 值班 gate
- 缺点:**要 LLM 主进程几个月不退**,不现实(会重启/compact/成本)

**模式 B:按需唤醒(推荐几个月维度)**
```
daemon 攒候选到阈值/够严重 → 写 pending_dispatch 信号(落盘)
  → 外部 cron/gateway 周期检测 → 有信号才拉起一个**短命研判 run**
  → 该 run:poll 候选 + 派子代理研判 + 上报 + 退出
  → 下次有信号再拉起。LLM 不常驻,按需活。
  → 已研判游标/已上报状态落盘,跨 run 接续不重不漏
```
- 优点:LLM 按需活,**几个月成本可控**,天然抗重启(每次短命 run)
- 缺点:要接 cron/gateway 调度 + 跨 run 状态接续
- daemon 那层(采集+初筛)照旧常驻,只有"研判"按需唤醒

**分工**:按源派(每源一研判子代理,带 profile.triage_hint)或按告警类型派。结论→主代理汇总→渐进上报(回到你最早画的主→子→孙 + 渐进上报场景)。

### 4.3 改动

- `daemon.py`:候选攒够触发 `pending_dispatch`(写信号文件/metrics 标记)
- 模式 B 需接 gateway/cron("检测信号→拉研判 run");模式 A 用现有 create_subagents
- 跨 run 状态:复用 `poll_cursor`(已读游标)+ 新增"已上报游标"

---

## 5. 改动点清单(文件级)

| 文件 | 改动 | 块 |
|---|---|---|
| `splitter.py` (新) | RecordSplitter 抽象 + line/json/multiline/delimiter | ② |
| `profiler.py` (新) | 采样逻辑(只读不推进) | ① |
| `collector.py` | collect_file 改调 splitter(line 逻辑抽成 LineSplitter,默认不变) | ② |
| `triage.py` | 对记录匹配 + field 抽取 + 正则安全校验 | ②③ |
| `store.py` | config 每源挂 profile;profile 读写;采样区 | ①③ |
| `daemon.py` | 按 profile 选 splitter+rules;候选攒够触发派发信号 | ②③ |
| `tools.py` | +log_source_sample / log_profile_set / log_profile_get | ① |
| `manager.py` | (模式 B)派发信号检测对接 | ③ |
| 测试 | 每切割器 no_loss+脏数据;profile 读写;正则安全 | 全 |

---

## 6. 风险表

| 风险 | 影响 | 缓解 |
|---|---|---|
| 切割器×增量×不丢出错 | 丢数据(最严重) | 复用 no_loss 测试范式 + 脏数据 + 跨边界记录测试 |
| LLM 正则 ReDoS/过宽 | daemon 卡死/初筛失效 | 正则校验 + 匹配超时 + 命中率监控 |
| LLM 学习质量差(采样偏) | 方案错→漏报/误报 | 头尾采样 + 试跑回看 + profile 可迭代闭环 |
| 几个月 LLM 调度/接续 | 研判断层/成本失控 | 模式 B 按需唤醒 + 状态落盘接续 |
| 格式漂移(上游改格式) | 切割/规则静默失效 | 命中率/解析率突变检测→告警重新探查 |

---

## 7. 建议落地顺序(若批准实施)

1. **块②切割器**(最硬、和已修 bug 同源,先夯实):splitter.py + collector 接入 + 全套 no_loss/脏数据测试。**这步独立可验证,不依赖 LLM**。
2. **块①探查器**:sample/profile 工具 + profiler.py。让 LLM 能看样本、写方案。
3. **块③a 数据驱动规则**:daemon 按 profile.rules 跑 + 正则安全。
4. **最小闭环真机测试**:1-2 源跑通"采样→LLM 定方案→daemon 执行→研判",验证范式。
5. **块③b 派发调度**:按拍板的模式 A/B 接线,扩到 15 源 + 几个月。

每步独立可测、可灰度,不破坏现有 log_ops。

---

## 8. 待你拍板的关键决策

1. **派发模式 A(主代理常驻值班)还是 B(按需唤醒)?** 几个月维度我强烈推荐 B。
2. **切割器初期支持哪几种?** 建议 line+json_lines+multiline_start+delimiter 一次做全(边际成本低,抽象统一)。
3. **profile 一次定死还是支持迭代闭环?** 推荐闭环(试跑回看再调)。
4. **LLM 生成正则的安全严格度?** 推荐:编译校验 + 匹配超时(硬性)+ 命中率监控(软告警)。
5. **落地顺序认可否?** 先夯块②(独立可验),再①③,最小闭环验证范式,最后扩规模。

# ADR-0003: Extension And Plugin System

LLM: Extensions must integrate through declared boundaries.

给人看的解释：
扩展通过 ExtensionPlugin 接口注册能力，不能直接 patch 核心模块。

## Status

Accepted

## Context

Growing extension code (log_analysis at 666+ lines) does not follow a consistent integration pattern. Extensions directly import and patch core modules. Tool registration is ad hoc. CLI commands require modifying `cli/parser.py`. No isolation: a buggy extension can break core. No discovery mechanism for new extensions.

## Decision

Define an `ExtensionPlugin` interface that all extensions implement. Core depends on the interface, not implementations.

```python
class ExtensionPlugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def register_tools(self, registry: ToolRegistry) -> None: ...

    def register_commands(self, parser: ArgumentParser) -> None: ...
    def on_startup(self, context: AgentContext) -> None: ...
    def on_shutdown(self) -> None: ...
```

Extensions are loaded only from the administrator's ordered `extension_plugins` list:

- `entrypoint:<name>` resolves exactly one installed `my_agent.plugins` entry point;
- `package.module:<attribute>` resolves an administrator-installed importable module.

`extensions_dir` is never scanned for executable Python. Missing, duplicate, invalid, or failing configured
plugins stop startup (fail-closed). Runtime activation order is tools → memory sources; CLI command
registration uses the same discovered registry. Each extension must document: name, owned files, runtime writes,
rollback plan.

## Consequences

- Clear contract: extensions know what they can register.
- Core depends on interface; extensions can be replaced without core changes.
- Discovery is explicit and deterministic; installation alone does not activate code.
- Current log_analysis security tools register through the canonical tool registry bootstrap; do not keep an unused LogAnalysisPlugin facade before extension discovery is wired.
- Extensions must not add top-level runtime directories or import from `cli/` directly.
- Reusable execution methods are Skills loaded by the canonical SkillsService; extensions do not register a
  second workflow-template router.

### References

- ADR-0001 (extensions layer); REFACTORING_BACKLOG.md item 8; ARCHITECTURE_EXEMPTIONS.md E-008

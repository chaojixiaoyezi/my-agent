from typing import Any

__all__ = [
    "WorkStateFieldSourceRequest",
    "WorkStateFieldSources",
    "build_work_state_field_sources",
    "source_work_state",
]


def __getattr__(name: str) -> Any:
    if name == "source_work_state":
        from .archive import source_work_state

        return source_work_state
    if name in {"WorkStateFieldSourceRequest", "WorkStateFieldSources", "build_work_state_field_sources"}:
        from . import sources

        return getattr(sources, name)
    raise AttributeError(name)

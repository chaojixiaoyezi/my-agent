from __future__ import annotations

"""Compatibility facade for subagent channel probe service."""

from .services.channel_probe import (
    SubAgentChannelProbeService,
    channel_probe_options,
    channel_probe_summary,
)

_channel_probe_options = channel_probe_options
_channel_probe_summary = channel_probe_summary


class SubAgentChannelProbeMixin:
    """Legacy mixin wrapper; new code should use SubAgentChannelProbeService."""

    def probe_channel(self, run_id):
        return self.channel_probe.probe_channel(run_id)

    def probe_channels(self, run_ids=None, *, params=None, limit=0):
        return self.channel_probe.probe_channels(run_ids, params=params, limit=limit)

    def write_channel_probe_report(self, run_ids=None, *, params=None, limit=0):
        return self.channel_probe.write_channel_probe_report(run_ids, params=params, limit=limit)

    def _probe_work_order_check(self, run_id, task, now):
        return self.channel_probe.probe_work_order_check(run_id, task, now)

    def _probe_json_files(self, task, now):
        return self.channel_probe.probe_json_files(task, now)

    def _update_task_from_probe(self, task, checks, now):
        return self.channel_probe.update_task_from_probe(task, checks, now)

    def _write_channel_probe_files(self, task, result):
        return self.channel_probe.write_channel_probe_files(task, result)


__all__ = [
    "SubAgentChannelProbeMixin",
    "SubAgentChannelProbeService",
    "_channel_probe_options",
    "_channel_probe_summary",
    "channel_probe_options",
    "channel_probe_summary",
]

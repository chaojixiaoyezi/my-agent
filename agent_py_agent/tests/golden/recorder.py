"""Capture raw source evidence alongside a causally ordered Gateway contract trace."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path


class Recorder:
    # LLM: Raw HTTP/files/provider payloads remain on disk; trace projection retains identity, epochs and outcomes.
    # 函数用途: Create a per-run evidence sink below the caller's temporary directory.
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.events = []
        self.sequences = defaultdict(int)
        self.raw_count = 0
        self.provider_offset = 0
        self.session_offsets = {}

    # LLM: Raw payloads are not compared as latency or display-format goldens; no raw evidence is discarded.
    # 函数用途: Persist a complete source payload and return its stable local evidence index.
    def raw(self, source, payload):
        self.raw_count += 1
        with (self.root / "raw.jsonl").open("a") as stream:
            stream.write(json.dumps({"index": self.raw_count, "source": source, "payload": payload}, sort_keys=True) + "\n")
        return self.raw_count

    # LLM: Every event carries all six causal namespace fields and a source-local increasing sequence.
    # 函数用途: Append one observed event without sorting or merging other sources.
    def event(self, source, kind, *, request_id=None, thread_id=None, execution_attempt_id=None,
              lease_epoch=0, tool_call_id=None, receipt_id=None, **fields):
        self.sequences[source] += 1
        row = {"source": source, "seq": self.sequences[source], "kind": kind,
               "request_id": request_id, "thread_id": thread_id, "execution_attempt_id": execution_attempt_id,
               "lease_epoch": lease_epoch, "tool_call_id": tool_call_id, "receipt_id": receipt_id,
               "timestamp": time.monotonic(), **fields}
        self.events.append(row)
        return {"source": source, "seq": row["seq"]}

    # LLM: The queue's actual execution fence is copied verbatim, never reconstructed from request ids.
    # 函数用途: Record one request lifecycle observation and retain the complete underlying file.
    def lifecycle(self, payload, kind, state, *, disposition=None, **fields):
        index = self.raw("queue", payload)
        return self.event("queue", kind, request_id=payload.get("id"),
            thread_id=payload.get("thread_id") or payload.get("metadata", {}).get("thread_id"),
            execution_attempt_id=payload.get("execution_attempt_id"), lease_epoch=payload.get("lease_epoch", 0),
            state=state, disposition=disposition, raw_index=index, **fields)

    # LLM: Provider requests include body hashes and actual transport ids; complete bodies stay in raw evidence.
    # 函数用途: Drain newly observed model calls in original provider order.
    def provider(self, provider, *, request_id=None, thread_id=None, execution_attempt_id=None, lease_epoch=0):
        with provider.condition:
            batch = list(provider.records[self.provider_offset:])
            end = self.provider_offset + len(batch)
        for row in batch:
            index = self.raw("provider", row)
            body = row["body"]
            outputs = []
            for digest in dict.fromkeys(re.findall(r"output_hash: ([0-9a-f]{64})", json.dumps(body))):
                candidates = list((self.root.parent / "gateway" / "home").rglob(f"*-{digest[:12]}.json"))
                payloads = [json.loads(path.read_text()) for path in candidates if path.parent.name == "tool_outputs"]
                matching = [payload for payload in payloads if payload.get("sha256") == digest]
                assert matching, f"missing actual referenced tool output: {digest}"
                content = matching[0]["content"]
                assert hashlib.sha256(content.encode()).hexdigest() == digest
                outputs.append({"sha256": digest, "content": content})
            self.event("provider", "model_request", request_id=request_id, thread_id=thread_id,
                execution_attempt_id=execution_attempt_id, lease_epoch=lease_epoch,
                method=row["method"], path=row["path"], body_hash=hashlib.sha256(json.dumps(body,
                    sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
                body_hash_basis="canonical_json_sha256", body=body, referenced_outputs=outputs, fixture_root=str(self.root.parent),
                source_root=str(Path(__file__).resolve().parents[3]),
                provider_origin=provider.base_url, gateway_origin=getattr(self, "gateway_origin", ""),
                connection_id=row["connection_id"], is_probe=row["is_probe"],
                message_roles=[message.get("role") for message in body.get("messages", [])], raw_index=index)
        self.provider_offset = end

    # LLM: Session rows keep their original per-file order; UI progress is not mistaken for a tool completion.
    # 函数用途: Capture durable conversation events and actual started/finished tool outcomes for one request.
    def session_and_tools(self, home, paths, fence):
        thread_id, request_id = fence["thread_id"], fence["request_id"]
        seen = set()
        for path in Path(home).rglob(f"{thread_id}.jsonl"):
            if path.parent.name != "messages" or path.resolve() in seen:
                continue
            seen.add(path.resolve())
            lines = path.read_text().splitlines()
            offset = self.session_offsets.get(path.resolve(), 0)
            for line in lines[offset:]:
                row = json.loads(line)
                index = self.raw("session_event", row)
                metadata = row.get("metadata", {})
                self.event("session", "message", **fence, event_id=row["message_id"], role=row["role"],
                    content=row["content"], channel=row.get("channel"), created_at=row["created_at"],
                    turn_end_reason=metadata.get("turn_end_reason"),
                    operation_verification=metadata.get("operation_verification"), raw_index=index)
            self.session_offsets[path.resolve()] = len(lines)
        for directory in (paths.processing, paths.done, paths.failed):
            chunk = directory / f"{request_id}.chunks.jsonl"
            if not chunk.exists():
                continue
            for line in chunk.read_text().splitlines():
                row = json.loads(line)
                progress = row.get("progress", {})
                if row.get("kind") == "tool_progress" and progress.get("phase") in {"started", "finished"}:
                    index = self.raw("tool_execution", row)
                    self.event("tool", "tool_result", **fence, tool_call_id=progress.get("call_id"),
                        tool=progress.get("tool"), phase=progress["phase"], round=progress.get("round"),
                        call_index=progress.get("call_index"), ok=progress.get("ok"),
                        handler_executed=progress.get("handler_executed"), error_code=progress.get("error_code"), raw_index=index)
                elif row.get("kind") == "permission_resolved":
                    index = self.raw("permission_resolved", row)
                    self.event("approval", "permission_resolved", **fence, decision=row.get("permission", {}).get("decision"), raw_index=index)

    # LLM: Persist the observed trace before normalization so mismatches can be independently inspected.
    # 函数用途: Save and return the unmodified event sequence.
    def finish(self):
        (self.root / "trace.raw.json").write_text(json.dumps(self.events, indent=2) + "\n")
        return self.events

"""S2-P1-01: converted frozen DTOs read every historical pickle layout and write the layout their old readers use.

Before Wave-1 the DTOs below were dict-backed ``@dataclass(frozen=True)`` classes whose pickle state is the instance
``__dict__``; ``Gap`` was already ``slots=True`` and used the standard-library positional list. The merged Wave-1
commit wrote the positional list for every converted class, and its standard-library reader paired a historical
dictionary's keys with the fields. The payloads below are trusted synthetic fixtures: the records built by
``_records()`` were pickled at their real module names by the exact class definitions of baseline ``d9caa95af``
(``_BASELINE_PAYLOADS``) and of the merged commit ``e6071c5ba`` (``_INTERIM_PAYLOADS``), each in its own process.
Protocol 4 and 5 payloads differ only in the protocol byte. Nothing here reads git history.
"""

from __future__ import annotations

import base64
import copy
import copyreg
import dataclasses
import importlib
import io
import pickle

import pytest

from agent_py_agent.agent.agent_core.tool_ir_history import _MarkedAssistantTurn
from agent_py_agent.agent.backends.keepalive_transport import KeepAliveRoute
from agent_py_agent.agent.backends.request_budget import RequestTokenBudget
from agent_py_agent.agent.backends.stream_parsers import StreamEvent
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.agent.common import fast_dataclass
from agent_py_agent.agent.common.fast_dataclass import fast_frozen
from agent_py_agent.agent.gateway_parts.request_event_hub import Gap
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall

_BASELINE_PAYLOADS = {
    ("budget", 4): (
        "gASVzgAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnJlcXVlc3RfYnVkZ2V0lIwSUmVxdWVz"
        "dFRva2VuQnVkZ2V0lJOUKYGUfZQojAxpbnB1dF90b2tlbnOUTbAEjA1vdXRwdXRfdG9rZW5zlE1AH4wVY29udGV4"
        "dF93aW5kb3dfdG9rZW5zlEpADQMAjA1zYWZldHlfdG9rZW5zlE0ABIwNc2hhcmVkX3dpbmRvd5SIjBVzZXJ2ZXJf"
        "bWFuYWdlZF9vdXRwdXSUiXViLg=="
    ),
    ("budget", 5): (
        "gAWVzgAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnJlcXVlc3RfYnVkZ2V0lIwSUmVxdWVz"
        "dFRva2VuQnVkZ2V0lJOUKYGUfZQojAxpbnB1dF90b2tlbnOUTbAEjA1vdXRwdXRfdG9rZW5zlE1AH4wVY29udGV4"
        "dF93aW5kb3dfdG9rZW5zlEpADQMAjA1zYWZldHlfdG9rZW5zlE0ABIwNc2hhcmVkX3dpbmRvd5SIjBVzZXJ2ZXJf"
        "bWFuYWdlZF9vdXRwdXSUiXViLg=="
    ),
    ("gap", 4): (
        "gASVVQAAAAAAAACMNGFnZW50X3B5X2FnZW50LmFnZW50LmdhdGV3YXlfcGFydHMucmVxdWVzdF9ldmVudF9odWKU"
        "jANHYXCUk5QpgZRdlChLB4mMB2V2aWN0ZWSUZWIu"
    ),
    ("gap", 5): (
        "gAWVVQAAAAAAAACMNGFnZW50X3B5X2FnZW50LmFnZW50LmdhdGV3YXlfcGFydHMucmVxdWVzdF9ldmVudF9odWKU"
        "jANHYXCUk5QpgZRdlChLB4mMB2V2aWN0ZWSUZWIu"
    ),
    ("marked_turn", 4): (
        "gASVlgIAAAAAAACML2FnZW50X3B5X2FnZW50LmFnZW50LmFnZW50X2NvcmUudG9vbF9pcl9oaXN0b3J5lIwUX01h"
        "cmtlZEFzc2lzdGFudFR1cm6Uk5QpgZR9lCiMBHRleHSUjAVoZWxsb5SMCnRvb2xfY2FsbHOUXZSMLmFnZW50X3B5"
        "X2FnZW50LmFnZW50LnRvb2xpbmcucnVudGltZV9jb250cmFjdHOUjAhUb29sQ2FsbJSTlCmBlH2UKIwHY2FsbF9p"
        "ZJSMBmNhbGwtMZSMCXRvb2xfbmFtZZSMCXJlYWRfZmlsZZSMCWFyZ3VtZW50c5R9lIwEcGF0aJSMBWEudHh0lHOM"
        "D3NvdXJjZV9wcm90b2NvbJSMBm5hdGl2ZZSMC3NjaGVtYV9oYXNolIxHc2hhMjU2OjAwMDAwMDAwMDAwMDAwMDAw"
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDCUjAZydW5faWSUjAZydW4tNDGU"
        "jAd0dXJuX2lklIwGdHVybi03lIwKYXR0ZW1wdF9pZJSMCWF0dGVtcHQtMpSMEnJlcXVpcmVkX2FjdGlvbl9pZJSM"
        "AJSMDG9wZXJhdGlvbl9pZJSML3Rvb2xfb3BlcmF0aW9uOmQ0YTAwNDVkNDZhM2VjMGZiNGZkYWVjMjhkNGU0OGFk"
        "lIwPaWRlbXBvdGVuY3lfa2V5lIwnaWRlbTpvcGVyYXRpb246Mzc3MzlmOWVhNGViMzZiMjE4MTU2ZmU1lIwPaW5w"
        "dXRfbWlncmF0aW9ulH2UdWJhjA5jb250ZW50X2Jsb2Nrc5RdlH2UKIwEdHlwZZRoBWgFaAZ1YXV9lIwLX3Rvb2xf"
        "cm91bmSUSwNzhpRiLg=="
    ),
    ("marked_turn", 5): (
        "gAWVlgIAAAAAAACML2FnZW50X3B5X2FnZW50LmFnZW50LmFnZW50X2NvcmUudG9vbF9pcl9oaXN0b3J5lIwUX01h"
        "cmtlZEFzc2lzdGFudFR1cm6Uk5QpgZR9lCiMBHRleHSUjAVoZWxsb5SMCnRvb2xfY2FsbHOUXZSMLmFnZW50X3B5"
        "X2FnZW50LmFnZW50LnRvb2xpbmcucnVudGltZV9jb250cmFjdHOUjAhUb29sQ2FsbJSTlCmBlH2UKIwHY2FsbF9p"
        "ZJSMBmNhbGwtMZSMCXRvb2xfbmFtZZSMCXJlYWRfZmlsZZSMCWFyZ3VtZW50c5R9lIwEcGF0aJSMBWEudHh0lHOM"
        "D3NvdXJjZV9wcm90b2NvbJSMBm5hdGl2ZZSMC3NjaGVtYV9oYXNolIxHc2hhMjU2OjAwMDAwMDAwMDAwMDAwMDAw"
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDCUjAZydW5faWSUjAZydW4tNDGU"
        "jAd0dXJuX2lklIwGdHVybi03lIwKYXR0ZW1wdF9pZJSMCWF0dGVtcHQtMpSMEnJlcXVpcmVkX2FjdGlvbl9pZJSM"
        "AJSMDG9wZXJhdGlvbl9pZJSML3Rvb2xfb3BlcmF0aW9uOmQ0YTAwNDVkNDZhM2VjMGZiNGZkYWVjMjhkNGU0OGFk"
        "lIwPaWRlbXBvdGVuY3lfa2V5lIwnaWRlbTpvcGVyYXRpb246Mzc3MzlmOWVhNGViMzZiMjE4MTU2ZmU1lIwPaW5w"
        "dXRfbWlncmF0aW9ulH2UdWJhjA5jb250ZW50X2Jsb2Nrc5RdlH2UKIwEdHlwZZRoBWgFaAZ1YXV9lIwLX3Rvb2xf"
        "cm91bmSUSwNzhpRiLg=="
    ),
    ("route", 4): (
        "gASVpAAAAAAAAACMMWFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLmtlZXBhbGl2ZV90cmFuc3BvcnSUjA5L"
        "ZWVwQWxpdmVSb3V0ZZSTlCmBlH2UKIwGc2NoZW1llIwFaHR0cHOUjARob3N0lIwQYXBpLmV4YW1wbGUudGVzdJSM"
        "BHBvcnSUTbsBjApwcm94eV9ob3N0lIwAlIwKcHJveHlfcG9ydJRLAHViLg=="
    ),
    ("route", 5): (
        "gAWVpAAAAAAAAACMMWFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLmtlZXBhbGl2ZV90cmFuc3BvcnSUjA5L"
        "ZWVwQWxpdmVSb3V0ZZSTlCmBlH2UKIwGc2NoZW1llIwFaHR0cHOUjARob3N0lIwQYXBpLmV4YW1wbGUudGVzdJSM"
        "BHBvcnSUTbsBjApwcm94eV9ob3N0lIwAlIwKcHJveHlfcG9ydJRLAHViLg=="
    ),
    ("stream_event", 4): (
        "gASVvAAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnN0cmVhbV9wYXJzZXJzlIwLU3RyZWFt"
        "RXZlbnSUk5QpgZR9lCiMB2NvbnRlbnSUjARwYXJ0lIwFdXNhZ2WUTowOdG9vbF91c2VfYmxvY2uUTowXYXNzaXN0"
        "YW50X2NvbnRlbnRfYmxvY2uUTowQdGhpbmtpbmdfY29udGVudJSMAJSME3Rvb2xfaW5wdXRfcHJvZ3Jlc3OUTnVi"
        "Lg=="
    ),
    ("stream_event", 5): (
        "gAWVvAAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnN0cmVhbV9wYXJzZXJzlIwLU3RyZWFt"
        "RXZlbnSUk5QpgZR9lCiMB2NvbnRlbnSUjARwYXJ0lIwFdXNhZ2WUTowOdG9vbF91c2VfYmxvY2uUTowXYXNzaXN0"
        "YW50X2NvbnRlbnRfYmxvY2uUTowQdGhpbmtpbmdfY29udGVudJSMAJSME3Rvb2xfaW5wdXRfcHJvZ3Jlc3OUTnVi"
        "Lg=="
    ),
    ("stream_event_shared", 4): (
        "gASV3gAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnN0cmVhbV9wYXJzZXJzlIwLU3RyZWFt"
        "RXZlbnSUk5QpgZR9lCiMB2NvbnRlbnSUjACUjAV1c2FnZZR9lCiMDGlucHV0X3Rva2Vuc5RLA4wNb3V0cHV0X3Rv"
        "a2Vuc5RLBXWMDnRvb2xfdXNlX2Jsb2NrlE6MF2Fzc2lzdGFudF9jb250ZW50X2Jsb2NrlE6MEHRoaW5raW5nX2Nv"
        "bnRlbnSUaAaME3Rvb2xfaW5wdXRfcHJvZ3Jlc3OUaAh1Yi4="
    ),
    ("stream_event_shared", 5): (
        "gAWV3gAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnN0cmVhbV9wYXJzZXJzlIwLU3RyZWFt"
        "RXZlbnSUk5QpgZR9lCiMB2NvbnRlbnSUjACUjAV1c2FnZZR9lCiMDGlucHV0X3Rva2Vuc5RLA4wNb3V0cHV0X3Rv"
        "a2Vuc5RLBXWMDnRvb2xfdXNlX2Jsb2NrlE6MF2Fzc2lzdGFudF9jb250ZW50X2Jsb2NrlE6MEHRoaW5raW5nX2Nv"
        "bnRlbnSUaAaME3Rvb2xfaW5wdXRfcHJvZ3Jlc3OUaAh1Yi4="
    ),
    ("tool_call", 4): (
        "gASV6gEAAAAAAACMLmFnZW50X3B5X2FnZW50LmFnZW50LnRvb2xpbmcucnVudGltZV9jb250cmFjdHOUjAhUb29s"
        "Q2FsbJSTlCmBlH2UKIwHY2FsbF9pZJSMBmNhbGwtMZSMCXRvb2xfbmFtZZSMCXJlYWRfZmlsZZSMCWFyZ3VtZW50"
        "c5R9lIwEcGF0aJSMBWEudHh0lHOMD3NvdXJjZV9wcm90b2NvbJSMBm5hdGl2ZZSMC3NjaGVtYV9oYXNolIxHc2hh"
        "MjU2OjAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
        "MDCUjAZydW5faWSUjAZydW4tNDGUjAd0dXJuX2lklIwGdHVybi03lIwKYXR0ZW1wdF9pZJSMCWF0dGVtcHQtMpSM"
        "EnJlcXVpcmVkX2FjdGlvbl9pZJSMAJSMDG9wZXJhdGlvbl9pZJSML3Rvb2xfb3BlcmF0aW9uOmQ0YTAwNDVkNDZh"
        "M2VjMGZiNGZkYWVjMjhkNGU0OGFklIwPaWRlbXBvdGVuY3lfa2V5lIwnaWRlbTpvcGVyYXRpb246Mzc3MzlmOWVh"
        "NGViMzZiMjE4MTU2ZmU1lIwPaW5wdXRfbWlncmF0aW9ulH2UdWIu"
    ),
    ("tool_call", 5): (
        "gAWV6gEAAAAAAACMLmFnZW50X3B5X2FnZW50LmFnZW50LnRvb2xpbmcucnVudGltZV9jb250cmFjdHOUjAhUb29s"
        "Q2FsbJSTlCmBlH2UKIwHY2FsbF9pZJSMBmNhbGwtMZSMCXRvb2xfbmFtZZSMCXJlYWRfZmlsZZSMCWFyZ3VtZW50"
        "c5R9lIwEcGF0aJSMBWEudHh0lHOMD3NvdXJjZV9wcm90b2NvbJSMBm5hdGl2ZZSMC3NjaGVtYV9oYXNolIxHc2hh"
        "MjU2OjAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
        "MDCUjAZydW5faWSUjAZydW4tNDGUjAd0dXJuX2lklIwGdHVybi03lIwKYXR0ZW1wdF9pZJSMCWF0dGVtcHQtMpSM"
        "EnJlcXVpcmVkX2FjdGlvbl9pZJSMAJSMDG9wZXJhdGlvbl9pZJSML3Rvb2xfb3BlcmF0aW9uOmQ0YTAwNDVkNDZh"
        "M2VjMGZiNGZkYWVjMjhkNGU0OGFklIwPaWRlbXBvdGVuY3lfa2V5lIwnaWRlbTpvcGVyYXRpb246Mzc3MzlmOWVh"
        "NGViMzZiMjE4MTU2ZmU1lIwPaW5wdXRfbWlncmF0aW9ulH2UdWIu"
    ),
}

_INTERIM_PAYLOADS = {
    ("assistant_turn", 4): (
        "gASVpgEAAAAAAACMJWFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnRvb2xfaXKUjA1Bc3Npc3RhbnRUdXJu"
        "lJOUKYGUXZQojAVoZWxsb5RdlIwuYWdlbnRfcHlfYWdlbnQuYWdlbnQudG9vbGluZy5ydW50aW1lX2NvbnRyYWN0"
        "c5SMCFRvb2xDYWxslJOUKYGUXZQojAZjYWxsLTGUjAlyZWFkX2ZpbGWUfZSMBHBhdGiUjAVhLnR4dJRzjAZuYXRp"
        "dmWUjEdzaGEyNTY6MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
        "MDAwMDAwMDAwMJSMBnJ1bi00MZSMBnR1cm4tN5SMCWF0dGVtcHQtMpSMAJSML3Rvb2xfb3BlcmF0aW9uOmQ0YTAw"
        "NDVkNDZhM2VjMGZiNGZkYWVjMjhkNGU0OGFklIwnaWRlbTpvcGVyYXRpb246Mzc3MzlmOWVhNGViMzZiMjE4MTU2"
        "ZmU1lH2UZWJhXZR9lCiMBHR5cGWUjAR0ZXh0lGgdaAV1YWViLg=="
    ),
    ("assistant_turn", 5): (
        "gAWVpgEAAAAAAACMJWFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnRvb2xfaXKUjA1Bc3Npc3RhbnRUdXJu"
        "lJOUKYGUXZQojAVoZWxsb5RdlIwuYWdlbnRfcHlfYWdlbnQuYWdlbnQudG9vbGluZy5ydW50aW1lX2NvbnRyYWN0"
        "c5SMCFRvb2xDYWxslJOUKYGUXZQojAZjYWxsLTGUjAlyZWFkX2ZpbGWUfZSMBHBhdGiUjAVhLnR4dJRzjAZuYXRp"
        "dmWUjEdzaGEyNTY6MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
        "MDAwMDAwMDAwMJSMBnJ1bi00MZSMBnR1cm4tN5SMCWF0dGVtcHQtMpSMAJSML3Rvb2xfb3BlcmF0aW9uOmQ0YTAw"
        "NDVkNDZhM2VjMGZiNGZkYWVjMjhkNGU0OGFklIwnaWRlbTpvcGVyYXRpb246Mzc3MzlmOWVhNGViMzZiMjE4MTU2"
        "ZmU1lH2UZWJhXZR9lCiMBHR5cGWUjAR0ZXh0lGgdaAV1YWViLg=="
    ),
    ("budget", 4): (
        "gASVXwAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnJlcXVlc3RfYnVkZ2V0lIwSUmVxdWVz"
        "dFRva2VuQnVkZ2V0lJOUKYGUXZQoTbAETUAfSkANAwBNAASIiWViLg=="
    ),
    ("budget", 5): (
        "gAWVXwAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnJlcXVlc3RfYnVkZ2V0lIwSUmVxdWVz"
        "dFRva2VuQnVkZ2V0lJOUKYGUXZQoTbAETUAfSkANAwBNAASIiWViLg=="
    ),
    ("route", 4): (
        "gASVcwAAAAAAAACMMWFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLmtlZXBhbGl2ZV90cmFuc3BvcnSUjA5L"
        "ZWVwQWxpdmVSb3V0ZZSTlCmBlF2UKIwFaHR0cHOUjBBhcGkuZXhhbXBsZS50ZXN0lE27AYwAlEsAZWIu"
    ),
    ("route", 5): (
        "gAWVcwAAAAAAAACMMWFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLmtlZXBhbGl2ZV90cmFuc3BvcnSUjA5L"
        "ZWVwQWxpdmVSb3V0ZZSTlCmBlF2UKIwFaHR0cHOUjBBhcGkuZXhhbXBsZS50ZXN0lE27AYwAlEsAZWIu"
    ),
    ("stream_event_shared", 4): (
        "gASVeAAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnN0cmVhbV9wYXJzZXJzlIwLU3RyZWFt"
        "RXZlbnSUk5QpgZRdlCiMAJR9lCiMDGlucHV0X3Rva2Vuc5RLA4wNb3V0cHV0X3Rva2Vuc5RLBXVOTmgFaAZlYi4="
    ),
    ("stream_event_shared", 5): (
        "gAWVeAAAAAAAAACMLGFnZW50X3B5X2FnZW50LmFnZW50LmJhY2tlbmRzLnN0cmVhbV9wYXJzZXJzlIwLU3RyZWFt"
        "RXZlbnSUk5QpgZRdlCiMAJR9lCiMDGlucHV0X3Rva2Vuc5RLA4wNb3V0cHV0X3Rva2Vuc5RLBXVOTmgFaAZlYi4="
    ),
    ("tool_call", 4): (
        "gASVQAEAAAAAAACMLmFnZW50X3B5X2FnZW50LmFnZW50LnRvb2xpbmcucnVudGltZV9jb250cmFjdHOUjAhUb29s"
        "Q2FsbJSTlCmBlF2UKIwGY2FsbC0xlIwJcmVhZF9maWxllH2UjARwYXRolIwFYS50eHSUc4wGbmF0aXZllIxHc2hh"
        "MjU2OjAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
        "MDCUjAZydW4tNDGUjAZ0dXJuLTeUjAlhdHRlbXB0LTKUjACUjC90b29sX29wZXJhdGlvbjpkNGEwMDQ1ZDQ2YTNl"
        "YzBmYjRmZGFlYzI4ZDRlNDhhZJSMJ2lkZW06b3BlcmF0aW9uOjM3NzM5ZjllYTRlYjM2YjIxODE1NmZlNZR9lGVi"
        "Lg=="
    ),
    ("tool_call", 5): (
        "gAWVQAEAAAAAAACMLmFnZW50X3B5X2FnZW50LmFnZW50LnRvb2xpbmcucnVudGltZV9jb250cmFjdHOUjAhUb29s"
        "Q2FsbJSTlCmBlF2UKIwGY2FsbC0xlIwJcmVhZF9maWxllH2UjARwYXRolIwFYS50eHSUc4wGbmF0aXZllIxHc2hh"
        "MjU2OjAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
        "MDCUjAZydW4tNDGUjAZ0dXJuLTeUjAlhdHRlbXB0LTKUjACUjC90b29sX29wZXJhdGlvbjpkNGEwMDQ1ZDQ2YTNl"
        "YzBmYjRmZGFlYzI4ZDRlNDhhZJSMJ2lkZW06b3BlcmF0aW9uOjM3NzM5ZjllYTRlYjM2YjIxODE1NmZlNZR9lGVi"
        "Lg=="
    ),
}

# Classes whose baseline definition the repaired output must stay readable by (Gap was already slots=True).
_BASELINE_LAYOUT_CLASSES = (KeepAliveRoute, StreamEvent, RequestTokenBudget, ToolCall, AssistantTurn, Gap)


# Function purpose: build the records the fixtures were written from, with the writer's constructor calls.
def _records() -> dict[str, object]:
    shared = {"input_tokens": 3, "output_tokens": 5}
    call = ToolCall(call_id="call-1", tool_name="read_file", arguments={"path": "a.txt"},
                    source_protocol="native", schema_hash="sha256:" + "0" * 64,
                    run_id="run-41", turn_id="turn-7", attempt_id="attempt-2")
    blocks = [{"type": "text", "text": "hello"}]
    return {
        "route": KeepAliveRoute(scheme="https", host="api.example.test", port=443),
        "stream_event": StreamEvent(content="part"),
        "stream_event_shared": StreamEvent(content="", usage=shared, tool_input_progress=shared),
        "budget": RequestTokenBudget(input_tokens=1200, output_tokens=8000, context_window_tokens=200000,
                                     safety_tokens=1024, shared_window=True),
        "tool_call": call,
        "gap": Gap(seq=7, reset_required=False, reason="evicted"),
        "assistant_turn": AssistantTurn(text="hello", tool_calls=[call], content_blocks=blocks),
        "marked_turn": _MarkedAssistantTurn(text="hello", tool_calls=[call], content_blocks=blocks, _tool_round=3),
    }


# Function purpose: compare records structurally: class name, then each field's value and value type, recursively.
def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = [(f.name, _plain(getattr(value, f.name)), type(getattr(value, f.name)).__name__)
                  for f in dataclasses.fields(value)]
        return type(value).__name__, fields, getattr(value, "_tool_round", None)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


# Function purpose: decode and load one embedded payload.
def _load(payload: str) -> object:
    return pickle.loads(base64.b64decode(payload))


# LLM: Emits the stream shape an older writer produced for a record: the class by reference, then BUILD(state).
#   Protocols 0 and 1 go through copyreg._reconstructor and later ones through NEWOBJ, as copyreg/object reduce did.
#   The pickler only accepts NEWOBJ for an instance of the class, so the target is an uninitialized instance.
# Class purpose: write an arbitrary historical state for one target object; every other object pickles normally.
class _HistoricalWriter(pickle.Pickler):
    def __init__(self, file, protocol: int, target: object, state: object) -> None:
        super().__init__(file, protocol)
        self._protocol = protocol
        self._target = target
        self._state = state

    def reducer_override(self, obj):
        if obj is not self._target:
            return NotImplemented
        if self._protocol < 2:
            return copyreg._reconstructor, (type(obj), object, None), self._state
        return copyreg.__newobj__, (type(obj),), self._state


# Function purpose: restore a class from a historical state through a real pickle stream.
def _restore(cls: type, state: object, protocol: int = pickle.HIGHEST_PROTOCOL) -> object:
    target = object.__new__(cls)
    buffer = io.BytesIO()
    _HistoricalWriter(buffer, protocol, target, state).dump(target)
    return pickle.loads(buffer.getvalue())


# Function purpose: the baseline definition of a converted class: same fields, frozen, no slots unless it had them.
def _baseline_layout(cls: type) -> type:
    fields = [(f.name, f.type, copy.copy(f)) for f in dataclasses.fields(cls)]
    return dataclasses.make_dataclass(cls.__name__, fields, frozen=True, slots=cls is Gap)


# LLM: Resolves each converted class to its baseline layout, as a baseline process would. Dict-backed layouts have no
#   __setstate__, so pickle updates the instance __dict__; the Gap layout keeps the standard-library positional reader.
# Class purpose: read repaired output the way the baseline definitions read it.
class _BaselineReader(pickle.Unpickler):
    def __init__(self, data: bytes, layouts: dict[tuple[str, str], type]) -> None:
        super().__init__(io.BytesIO(data))
        self._layouts = layouts

    def find_class(self, module, name):
        layout = self._layouts.get((module, name))
        return layout if layout is not None else super().find_class(module, name)


@pytest.mark.parametrize("key", sorted(_BASELINE_PAYLOADS), ids=lambda key: f"{key[0]}-p{key[1]}")
def test_baseline_payload_restores_every_field_by_name(key):
    name, _protocol = key
    restored = _load(_BASELINE_PAYLOADS[key])
    assert type(restored) is type(_records()[name])
    assert _plain(restored) == _plain(_records()[name])


@pytest.mark.parametrize("key", sorted(_INTERIM_PAYLOADS), ids=lambda key: f"{key[0]}-p{key[1]}")
def test_interim_positional_payload_restores_every_field(key):
    name, _protocol = key
    restored = _load(_INTERIM_PAYLOADS[key])
    assert type(restored) is type(_records()[name])
    assert _plain(restored) == _plain(_records()[name])


@pytest.mark.parametrize("protocol", (4, 5))
def test_audited_baseline_values_keep_their_types(protocol):
    route = _load(_BASELINE_PAYLOADS[("route", protocol)])
    assert route.port == 443 and type(route.port) is int and route.proxy_host == ""
    assert _load(_BASELINE_PAYLOADS[("stream_event", protocol)]).content == "part"
    budget = _load(_BASELINE_PAYLOADS[("budget", protocol)])
    assert budget.context_window_tokens == 200000 and type(budget.context_window_tokens) is int
    assert _load(_BASELINE_PAYLOADS[("tool_call", protocol)]).run_id == "run-41"
    event = _load(_BASELINE_PAYLOADS[("stream_event", protocol)])
    assert event.usage is None and event.tool_use_block is None and event.thinking_content == ""
    marked = _load(_BASELINE_PAYLOADS[("marked_turn", protocol)])
    assert type(marked) is _MarkedAssistantTurn and marked._tool_round == 3
    assert marked.tool_calls[0].run_id == "run-41"


@pytest.mark.parametrize("payloads", (_BASELINE_PAYLOADS, _INTERIM_PAYLOADS), ids=("baseline", "interim"))
@pytest.mark.parametrize("protocol", (4, 5))
def test_restored_records_keep_shared_references(payloads, protocol):
    event = _load(payloads[("stream_event_shared", protocol)])
    assert event.usage == {"input_tokens": 3, "output_tokens": 5}
    assert event.usage is event.tool_input_progress


@pytest.mark.parametrize("protocol", (0, 2, 4, 5))
@pytest.mark.parametrize("name", ["route", "stream_event", "stream_event_shared", "budget", "tool_call", "gap",
                                  "assistant_turn"])
def test_repaired_output_is_read_by_the_baseline_definitions(name, protocol):
    record = _records()[name]
    layouts = {(cls.__module__, cls.__qualname__): _baseline_layout(cls) for cls in _BASELINE_LAYOUT_CLASSES}
    restored = _BaselineReader(pickle.dumps(record, protocol), layouts).load()
    assert type(restored) is layouts[(type(record).__module__, type(record).__qualname__)]
    assert ("__dict__" in vars(type(restored))) is (type(record) is not Gap)
    assert _plain(restored) == _plain(record)
    if name == "stream_event_shared":
        assert restored.usage is restored.tool_input_progress


def test_converted_classes_write_named_state_and_gap_keeps_the_positional_list():
    assert _records()["route"].__getstate__() == {
        "scheme": "https", "host": "api.example.test", "port": 443, "proxy_host": "", "proxy_port": 0,
    }
    assert list(_records()["tool_call"].__getstate__()) == [f.name for f in dataclasses.fields(ToolCall)]
    assert _records()["gap"].__getstate__() == [7, False, "evicted"]


def test_positional_state_table_names_converted_classes_with_the_standard_writer():
    assert fast_dataclass._POSITIONAL_STATE_CLASSES
    for qualified in fast_dataclass._POSITIONAL_STATE_CLASSES:
        module_name, _, qualname = qualified.rpartition(".")
        cls = getattr(importlib.import_module(module_name), qualname)
        assert cls in fast_dataclass.CONVERTED_CLASSES, qualified
        assert cls.__getstate__ is dataclasses._dataclass_getstate, qualified
        assert cls.__setstate__ is fast_dataclass._restore_state, qualified


@pytest.mark.parametrize("protocol", (0, 2, 5))
def test_slot_pair_state_restores_by_name(protocol):
    route = _records()["route"]
    named = route.__getstate__()
    split = ({"scheme": "https", "host": "api.example.test"}, {"port": 443, "proxy_host": "", "proxy_port": 0})
    for state in ((None, named), split, (named, None)):
        assert _restore(KeepAliveRoute, state, protocol) == route


def test_missing_fields_take_their_declared_defaults():
    route = _restore(KeepAliveRoute, {"scheme": "https", "host": "h", "port": 443})
    assert (route.proxy_host, route.proxy_port) == ("", 0)
    assert _restore(KeepAliveRoute, ["https", "h", 443]) == route
    first, second = (_restore(AssistantTurn, {"text": "x"}) for _ in range(2))
    assert first.tool_calls == [] and first.content_blocks == []
    assert first.tool_calls is not second.tool_calls


@pytest.mark.parametrize(("state", "message"), [
    ({"scheme": "https", "host": "h", "port": 1, "zz_unknown": 1}, r"unknown fields \['zz_unknown'\]"),
    ({"host": "h", "port": 1}, "no value for field 'scheme'"),
    (["https", "h", 1, "", 0, "extra"], "6 values for 5 fields"),
    ("https", "unsupported pickle state str"),
    (("https", "h"), "unsupported pickle state tuple"),
    (({"scheme": "https"}, ["h"]), "unsupported pickle state tuple"),
    (None, "unsupported pickle state NoneType"),
])
def test_malformed_state_fails_before_any_field_is_written(state, message):
    target = object.__new__(KeepAliveRoute)
    with pytest.raises(TypeError, match=message):
        target.__setstate__(state)
    assert [f.name for f in dataclasses.fields(KeepAliveRoute) if hasattr(target, f.name)] == []
    if state is not None:
        with pytest.raises(TypeError, match=message):
            _restore(KeepAliveRoute, state)


def test_class_body_state_functions_are_left_in_place():
    assert _MarkedAssistantTurn.__dict__["__getstate__"] is object.__getstate__
    assert _MarkedAssistantTurn.__setstate__ is not fast_dataclass._restore_state

    @fast_frozen
    @dataclasses.dataclass(frozen=True, slots=True)
    class OwnReader:
        a: int = 0

        def __setstate__(self, state):
            object.__setattr__(self, "a", state[0] + 1)

    assert OwnReader in fast_dataclass.CONVERTED_CLASSES
    assert OwnReader.__getstate__ is dataclasses._dataclass_getstate
    assert copy.copy(OwnReader(1)).a == 2


def test_copy_and_deepcopy_keep_values_and_shared_references():
    event = _records()["stream_event_shared"]
    shallow, deep = copy.copy(event), copy.deepcopy(event)
    assert shallow == event and shallow.usage is event.usage
    assert deep == event and deep.usage is deep.tool_input_progress and deep.usage is not event.usage
    turn = _records()["assistant_turn"]
    assert copy.deepcopy(turn) == turn and copy.deepcopy(turn).tool_calls[0] is not turn.tool_calls[0]


def test_unconverted_subclass_state_covers_its_own_fields():
    @dataclasses.dataclass(frozen=True)
    class LabelledRoute(KeepAliveRoute):
        label: str = ""

    item = LabelledRoute("https", "h", 1, label="primary")
    assert item.__getstate__()["label"] == "primary"
    assert copy.copy(item) == item and copy.deepcopy(item) == item

"""Routines: one phrase ("Nova, gaming mode") runs several tools in order.

Defined in config (config.yaml has examples; add your own in config/local.yaml). Every step
goes through the normal registry, so confirmations, remote blocks and the audit log still
apply: a routine can't do anything its steps couldn't do on their own.
"""

from __future__ import annotations

import asyncio
import re

from assistant.core.config import RoutineConfig, Settings
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry
from assistant.voice.textnorm import normalize_words

MAX_WAIT_S = 30
_VERBS = re.compile(r"^(?:(?:start|begin|run|do|activate|enable|turn on|switch to|switch on|go into|go to|"
                    r"put (?:me|the pc|it) in(?:to)?|set)\s+)?(?:(?:the|my)\s+)?")
_TAIL = re.compile(r"\s+(?:routine|please|now)$")


def active(settings: Settings) -> dict[str, RoutineConfig]:
    """Routines from config, plus the ones taught by showing (data/learned.json)."""
    from assistant.tools.teach import load_learned
    out = {k: v for k, v in settings.routines.items() if v is not None}
    for k, v in load_learned().items():
        if k not in out:
            try:
                out[k] = RoutineConfig.model_validate(v)
            except Exception:
                pass
    return out


def _norm(text: str) -> str:
    t = " ".join(normalize_words(text))
    t = _VERBS.sub("", t, count=1)
    while True:
        new = _TAIL.sub("", t)
        if new == t:
            return t
        t = new


def match_routine(text: str, settings: Settings) -> tuple[str, dict] | None:
    """Exact phrase match (after 'start'/'turn on' and similar are removed): no guessing."""
    said = _norm(text)
    if not said:
        return None
    for name, r in active(settings).items():
        for phrase in [*r.phrases, name.replace("_", " ")]:
            if said == _norm(phrase):
                return "run_routine", {"name": name}
    return None


def problems(settings: Settings, reg: ToolRegistry) -> list[str]:
    """Config mistakes, shown by `python -m assistant routines` and logged at startup."""
    out = []
    for name, r in active(settings).items():
        if not r.steps:
            out.append(f"{name}: has no steps")
        for i, step in enumerate(r.steps, 1):
            if step.tool is None:
                continue
            if step.tool == "run_routine":
                out.append(f"{name} step {i}: a routine can't run another routine")
            elif reg.get(step.tool) is None:
                out.append(f"{name} step {i}: unknown tool '{step.tool}' (see: python -m assistant tools)")
    return out


def register(reg: ToolRegistry) -> None:
    routines = active(reg.settings)

    async def run_routine(args: dict, ctx: ToolContext) -> str:
        name = args["name"]
        r = active(ctx.settings).get(name)
        if r is None:
            raise ToolError(f"There's no routine called {name}.")
        failed = []
        for step in r.steps:
            if step.tool is None:
                await asyncio.sleep(min(max(step.wait, 0), MAX_WAIT_S))
                continue
            if step.tool == "run_routine":
                failed.append("a step that runs another routine")
                continue
            res = await reg.execute(step.tool, dict(step.args), ctx)
            if res.is_error and not step.optional:
                what = reg.describe(step.tool, step.args)
                why = "you said no" if "declined" in str(res.content) else str(res.content).rstrip(".")
                failed.append(f"{what} ({why})")
        title = name.replace("_", " ")
        reply = r.reply.strip() or f"{title[0].upper()}{title[1:]} is done."
        if not failed:
            return reply
        if len(failed) == len([s for s in r.steps if s.tool and not s.optional]):
            return f"None of {title} worked: " + "; ".join(failed) + "."
        return reply + " Except: " + "; ".join(failed) + "."

    lines = [f"{n.replace('_', ' ')} ({', '.join(r.phrases[:2]) or n})" for n, r in routines.items()]
    reg.tool(
        "run_routine",
        "Run one of the user's routines (several actions at once). Routines: " + ("; ".join(lines) or "none yet") + ".",
        # No fixed list of names: lessons taught while Nova runs must work straight away.
        {"type": "object", "properties": {"name": {"type": "string", "maxLength": 60}},
         "required": ["name"], "additionalProperties": False},
        risk=Risk.SAFE, category="routines",
        describe=lambda a: f"run the {a.get('name', '').replace('_', ' ')} routine",
    )(run_routine)

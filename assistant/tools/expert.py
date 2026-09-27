"""Hand-off from the everyday model to the expert (your Claude subscription, or the API)."""

from __future__ import annotations

import asyncio

from assistant.brain.expert import ExpertError
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry


async def escalate(args: dict, ctx: ToolContext) -> str:
    brain = ctx.services.get("brain")
    if brain is None:
        raise ToolError("Expert model is not available.")
    try:
        return await brain.ask_expert(args["task"], args.get("context", ""))
    except ExpertError as e:
        raise ToolError(str(e)) from e
    except asyncio.TimeoutError as e:
        raise ToolError("The expert took too long and was stopped.") from e


def register(reg: ToolRegistry) -> None:
    reg.tool(
        "escalate",
        "Hand a hard task to Claude, a much stronger (but slower) model that can also browse "
        "the web: research, comparisons, analysis, writing, code, maths, planning, or when the "
        "user says 'ask Claude'. It cannot see this conversation, so include everything it "
        "needs in task/context. Summarize its answer briefly for speech.",
        {
            "type": "object",
            "properties": {
                "task": {"type": "string", "minLength": 1, "maxLength": 20000},
                "context": {"type": "string", "maxLength": 50000},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        risk=Risk.SAFE, category="brain",
    )(escalate)

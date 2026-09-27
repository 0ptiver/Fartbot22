"""Hand-off from the fast chat model to the stronger expert model."""

from __future__ import annotations

from vesper.tools.registry import Risk, ToolContext, ToolError, ToolRegistry


async def escalate(args: dict, ctx: ToolContext) -> str:
    brain = ctx.services.get("brain")
    if brain is None:
        raise ToolError("Expert model is not available.")
    return await brain.ask_expert(args["task"], args.get("context", ""))


def register(reg: ToolRegistry) -> None:
    reg.tool(
        "escalate",
        "Hand a hard problem to a stronger, slower expert model: multi-step reasoning, "
        "maths, code, planning, or detailed analysis. Pass everything it needs; it cannot "
        "see the conversation. Summarize its answer briefly for speech.",
        {
            "type": "object",
            "properties": {
                "task": {"type": "string", "minLength": 1},
                "context": {"type": "string"},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        risk=Risk.SAFE, category="brain",
    )(escalate)

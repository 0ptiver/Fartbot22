"""Timer, reminder and alarm tools (backed by core.scheduler)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from assistant.core.scheduler import Scheduler, human_duration, parse_clock
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry


def _sched(ctx: ToolContext) -> Scheduler:
    s = ctx.services.get("scheduler")
    if s is None:
        raise ToolError("Timers aren't available in this mode.")
    return s


def _seconds(args: dict) -> float:
    return (float(args.get("hours") or 0) * 3600 + float(args.get("minutes") or 0) * 60
            + float(args.get("seconds") or 0))


def set_timer(args: dict, ctx: ToolContext) -> str:
    secs = _seconds(args)
    if secs <= 0:
        raise ToolError("How long should the timer be?")
    if secs > 7 * 86400:
        raise ToolError("That's longer than a week; set a reminder for a time instead.")
    s = _sched(ctx)
    s.add("timer", s.clock() + secs, args.get("label", ""))
    label = f" for the {args['label'].strip()}" if args.get("label") else ""
    return f"Timer set{label}: {human_duration(secs)}."


def _due(args: dict, ctx: ToolContext) -> tuple[float, str]:
    tz = ctx.settings.assistant.timezone
    secs = _seconds(args)
    if args.get("at"):
        try:
            when = parse_clock(args["at"], tz)
        except ValueError as e:
            raise ToolError(str(e)) from e
        now = datetime.now(ZoneInfo(tz))
        day = "today" if when.date() == now.date() else "tomorrow"
        return when.timestamp(), f"at {when.strftime('%I:%M %p').lstrip('0')} {day}"
    if secs > 0:
        return _sched(ctx).clock() + secs, f"in {human_duration(secs)}"
    raise ToolError("When? Give me a time like 7:30 pm, or how long from now.")


def set_reminder(args: dict, ctx: ToolContext) -> str:
    due, when = _due(args, ctx)
    _sched(ctx).add("reminder", due, args["text"])
    return f"I'll remind you {when}: {args['text'].strip()}."


def set_alarm(args: dict, ctx: ToolContext) -> str:
    due, when = _due(args, ctx)
    _sched(ctx).add("alarm", due, args.get("label", ""))
    return f"Alarm set for {when.removeprefix('at ')}."


def list_timers(args: dict, ctx: ToolContext) -> str:
    s = _sched(ctx)
    items = s.upcoming()
    if not items:
        return "No timers, reminders or alarms are set."
    tz = ctx.settings.assistant.timezone
    lines = []
    for r in items[:8]:
        left = r.due - s.clock()
        at = datetime.fromtimestamp(r.due, ZoneInfo(tz)).strftime("%I:%M %p").lstrip("0")
        what = f" ({r.text})" if r.text else ""
        lines.append(f"{r.kind}{what}: {human_duration(left)} left, at {at}")
    return "; ".join(lines) + "."


def cancel_timer(args: dict, ctx: ToolContext) -> str:
    hits = _sched(ctx).cancel(args.get("which", ""))
    if not hits:
        return "There's nothing like that to cancel."
    if len(hits) == 1:
        r = hits[0]
        return f"Cancelled the {r.kind}" + (f" for {r.text}." if r.text else ".")
    return f"Cancelled {len(hits)} timers and reminders."


def register(reg: ToolRegistry) -> None:
    dur = {"hours": {"type": "number", "minimum": 0}, "minutes": {"type": "number", "minimum": 0},
           "seconds": {"type": "number", "minimum": 0}}
    reg.tool("set_timer", "Start a countdown timer. Give hours/minutes/seconds and an optional label "
             "(e.g. 'pasta').",
             {"type": "object", "properties": {**dur, "label": {"type": "string", "maxLength": 60}},
              "additionalProperties": False}, risk=Risk.SAFE, category="time")(set_timer)
    reg.tool("set_reminder", "Remind the user about something later: either at a clock time "
             "(at='7:30 pm') or after a duration (minutes=20).",
             {"type": "object", "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 200},
                                               "at": {"type": "string", "maxLength": 20}, **dur},
              "required": ["text"], "additionalProperties": False}, risk=Risk.SAFE, category="time")(set_reminder)
    reg.tool("set_alarm", "Set an alarm for a clock time (at='7 am'), with an optional label.",
             {"type": "object", "properties": {"at": {"type": "string", "maxLength": 20},
                                               "label": {"type": "string", "maxLength": 60}, **dur},
              "additionalProperties": False}, risk=Risk.SAFE, category="time")(set_alarm)
    reg.tool("list_timers", "List running timers, reminders and alarms.",
             risk=Risk.SAFE, category="time")(list_timers)
    reg.tool("cancel_timer", "Cancel a timer/reminder/alarm. which = words from its label, 'timer', "
             "'alarm', 'reminder' or 'all'. Empty = the next one due.",
             {"type": "object", "properties": {"which": {"type": "string", "maxLength": 60}},
              "additionalProperties": False}, risk=Risk.SAFE, category="time")(cancel_timer)

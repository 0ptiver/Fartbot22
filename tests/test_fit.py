"""Owner: "I can't use him for more than 5 minutes without him bugging out and not knowing what
he is doing". The system prompt + tools are ~5,200 of the model's 8,192 tokens; past that, Ollama
quietly dropped the oldest messages, mid tool loop even the request itself. Now Nova decides
what's left out, and the current request is always sent."""

from assistant.brain import fit
from assistant.brain.local import LocalBrain
from assistant.tools import build_registry


def ctx(n=0):
    return f"<context>\ntime: Sunday\nwindow in front (keys and typing go here): Firefox - page {n}\n</context>\n"


def session(turns=30, tool_chars=4000):
    msgs = []
    for n in range(turns):
        msgs += [{"role": "user", "content": ctx(n) + f"request {n}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "browser", "arguments": {}}}]},
                 {"role": "tool", "tool_name": "browser", "content": f"page {n} " + "[1] link \"x\" " * (tool_chars // 14)},
                 {"role": "assistant", "content": f"Done {n}."}]
    return msgs


def test_a_short_chat_is_sent_as_it_is_apart_from_stale_context():
    msgs = session(2, 100)
    out = fit.fit(msgs, 10_000)
    assert len(out) == len(msgs)
    assert "<context>" not in out[0]["content"] and out[0]["content"].endswith("request 0")   # old screen: gone
    assert out[4]["content"].startswith("<context>")                                            # current: kept


def test_a_long_session_keeps_the_request_and_fits():
    msgs = session(30)
    budget = 2000
    out = fit.fit(msgs, budget)
    assert sum(fit.tokens(m) for m in out) <= budget
    requests = [m["content"] for m in out if fit.is_request(m)]
    assert requests[-1].startswith("<context>") and requests[-1].endswith("request 29")        # never lost
    assert out[0]["role"] == "user"                                                             # starts on a turn
    assert msgs[-2]["content"] != out[-2]["content"] or len(out[-2]["content"]) >= fit.MIN_TOOL_CHARS


def test_old_tool_results_are_shortened_before_turns_are_dropped():
    msgs = session(3, 3000)
    out = fit.fit(msgs, 2200)
    assert [m["content"] for m in out if fit.is_request(m)][-1].endswith("request 2")
    assert len(out) == len(msgs)                                  # all three turns still there...
    assert len(out[2]["content"]) < 400                           # ...with the old page listings cut
    assert out[-2]["content"] == msgs[-2]["content"]              # the current one whole


def test_a_huge_page_in_this_turn_is_cut_not_the_request():
    msgs = session(1, 40_000)
    out = fit.fit(msgs, 2000)
    assert out[0] == msgs[0] and len(out[2]["content"]) >= fit.MIN_TOOL_CHARS
    assert sum(fit.tokens(m) for m in out) <= 2000 + 50


def test_nudges_are_not_requests_and_not_sent_with_their_mark():
    msgs = [{"role": "user", "content": ctx() + "open steam"},
            {"role": "assistant", "content": "Opening Steam."},
            {"role": "user", "nudge": True, "content": "Call the tool."}]
    out = fit.fit(msgs, 10_000)
    assert out[0]["content"].startswith("<context>") and "nudge" not in out[2]


def test_the_real_budget_leaves_room_for_turns(settings):
    brain = LocalBrain(settings, build_registry(settings))
    long = session(40, 400)
    out = brain._fitted(long, True)
    assert fit.is_request(out[0])
    requests = [m for m in out if fit.is_request(m)]
    assert requests[-1]["content"].endswith("request 39") and len(requests) >= 5    # a few turns of memory


async def test_a_long_session_never_loses_the_request(local_settings, ctx):
    """40 turns: big browser pages, fast commands and questions mixed. Every request the model
    gets fits the window, starts on a request, and ends with what was just asked."""
    from assistant.core.conversation import Conversation
    from tests.test_local_brain import collect, make, text_reply, tool_reply
    replies = []
    for n in range(40):
        if n % 2 == 0:
            replies += [tool_reply("browser", {"action": "look"}), text_reply(f"Page {n} has lots of links, sir.")]
        else:
            replies += [text_reply(f"Answer {n}, sir.")]
    brain, fake = make(local_settings, replies)
    brain.registry._tools["browser"].handler = lambda a, c: "Now on a page. " + " ".join(f'[{i}] link "item {i}"' for i in range(300))
    brain.registry._tools["volume"].handler = lambda a, c: "Volume 40."
    conv = Conversation(max_turns=local_settings.brain.history_turns)
    fixed = fit.tokens(brain._system) + fit.tokens(brain.tools())
    for n in range(40):
        await collect(brain, conv, f"tell me about the thing number {n} on this page", ctx)
        await collect(brain, conv, "volume up", ctx)                           # fast path in between
    assert not fake.replies                                                    # every model reply was used
    for _, body in fake.requests:
        msgs = body["messages"][1:]
        asked = [m for m in msgs if m["role"] == "user" and m["content"].startswith("<context>")]
        assert len(asked) == 1 and "tell me about the thing number" in asked[0]["content"]   # the current request
        assert msgs[0]["role"] == "user"
        assert fixed + sum(fit.tokens(m) for m in msgs) <= local_settings.brain.local.num_ctx

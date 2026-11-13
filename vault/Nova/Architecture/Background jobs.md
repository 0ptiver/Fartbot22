---
tags: [nova, architecture, claude, jobs]
---
# Background jobs

`assistant/brain/jobs.py` (`Job`, `Jobs`) + `LocalBrain._start_job`. Whole jobs run **in the background** so Nova keeps talking.

## Starting one
"Complete this assignment for me", "do my homework", "answer these questions", "fill out this survey", "do what's on my screen" (`local._SCREEN_TASK` + `_TASK_NOUN`, via `_whole_job`). Extra instructions on the end (", don't submit it", ", start at question 3") stay part of the job (`_JOB_EXTRAS`). "Use Claude to <whole job>" counts too.
- The turn ends at once: "On it, sir. I'll work through it while we talk..."
- One job at a time. The job runs `Agent.run(long=True)` as its own asyncio task, with `TASK_SYSTEM` instructions: find it (window or browser tab, ignore Nova's own window), read **all** requirements, decide where answers go (in the page, else `write_document` → Documents\Nova in Notepad), do and check every part, call `progress` after each part. No final Submit/Send of graded or outgoing work unless told. No passwords, payment, ID or contact details. Stop at sign-ins and CAPTCHAs.

## While it runs
| You say | Nova |
|---|---|
| "Are you still working?", "how far?", "what are you doing?" (`jobs.STATUS`) | answers from the job's real progress (`Job.spoken`), never a guess |
| "Stop", "Nova, stop working" (`jobs.STOP`) | stops it |
| "Also...", "make sure...", "use Word instead" (`jobs.ADD_ON`) | restarts it with `Job.resume()`: progress so far + every added instruction |
| anything else | normal chat; the model's context says "your background job" |

Steps still reach the HUD's live feed. The result is announced (`voice.announce`) and added to the conversation.

## When Claude runs out of steps
Each Claude run gets 250 steps. If it uses them all (`OutOfSteps`), Nova starts a **fresh run** with `Job.carry_on()` (progress so far, what it saved, added instructions), up to `brain.agent.task_runs` (4) runs. Then it says plainly why it stopped.

## Afterwards
Follow-ups to a finished job ("shorten the answers", "rewrite them like a high schooler", "paste the shortened ones"; `jobs.FOLLOW_UP`, unless `match_intent` claims it, e.g. "turn it up") start a new job with `Job.handover()`: the last task, result and the exact text it saved. Jobs are not learned as shortcuts.

> [!warning] Declined
> Pacing quiz answers so automation "doesn't look robotic" is detection evasion and won't be built. General job fixes are fine.

Related: [[Work it out]] · [[Lessons learned]]

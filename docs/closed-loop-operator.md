# Board-owned closed-loop operator

The existing dispatcher reconciles lifecycle transitions before selecting work. It
does not add a scheduler or another runnable store. Opt in through `operator` in
the bound board's existing `board.json` (written with `write_board_metadata`).
This overrides profile-local `kanban.operator` rules for that board only;
credentials and tool authority remain profile-scoped.

Default budgets: 900 seconds per new attempt, 600 seconds without an owned-run
checkpoint, 7200 seconds from the original request, 900 seconds for rework, three
failed review cycles, and one independently reviewed repair per exhausted task.
Events retain fixed due times. Heartbeats and retries cannot renew them. Set
`activation_at` at rollout: old workers did not receive the checkpoint contract.
Transitions are sampled by the existing dispatcher interval (three seconds on the
takeover board); runtime is not a claim that an external service will cooperate.

Deadline intake creates a task with authenticated original text and exact return
route. An interrupted foreground worker holds a continuation fence until its
worker-done event confirms shutdown. Missing ownership stays in the existing
intake journal for idempotent recovery on dispatcher ticks. An unverified PID or
surviving worker tree retains its execution claim and produces an agent-owned
exception; never release it beside a second worker. A genuinely unrepairable
execution fence remains an exception, not a Harry question.

Exhausted retries and failed rework produce one bounded repair with a verified
dispatchable owner. A done flag cannot release its original task: the repair
must have a review handoff and completion by a different profile. Rejected
candidates need changed artifact bytes, checkpoint or acceptance evidence before
another review. Cosmetic summary edits cannot satisfy that check. A malformed
operator row cannot prevent native reclaim and queue selection.

Delivery persists independent text and artifact cursors, hashes, route and
transport message ID. Failed delivery retains the subscription and backs off
10-300 seconds. A reply by the authenticated original user to the exact completed
result records receipt separately. Neither successful send nor a reply is an
acceptance verdict. Independent platform readback and outcome reproduction are
still required for takeover parity.

Safe release is owned by the Jarvis Windows controller:
`C:/Users/User/DiscordBots/Jarvis/scripts/hermes_gateway_launch.py`. Existing
service entrypoints consume its sealed runtime selector. The candidate has its
own interpreter/source directory; the editable checkout and its saved unfinished
changes are preserved. Manifest selection is deployment intent; serving process
identity, health, request transitions and platform readback prove execution.

Original requests cannot self-certify completion: a different profile must hold
a native review run. Canonical results retain the substantive review answer. An
internal dependency missing a formal link remains an agent-owned hold. The
operator reconciles one exact referenced parent through the native cycle guard;
a missing, ambiguous or cyclic reference gets one bounded reviewed repair. It
never guesses away an explicit needs_input choice.

All native Windows restart entrypoints honor the same sealed selector and hash
of the selected controller as the existing service paths. Worker import is
verified outside the source root with PYTHONPATH/cwd disabled; gateway imports
alone are insufficient. Runtime preparation must bind only its own interpreter
to its own release source, never repoint the editable install.

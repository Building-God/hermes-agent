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

A repair's total deadline stops further goal-mode attempts through the native
external block path, which verifies the entire worker tree before clearing a
claim. Refused or unknown process identities retain the claim and original hold.
An exhausted implementation remains an agent exception and cannot spawn another
repair. Independent review is a separate fixed phase: its first native handoff
gets 900 seconds, unaffected by heartbeats or repeated narration. A historical
repair incorrectly held as needs_input can hand its existing candidate to a real
reviewer without restarting implementation or accepting the candidate. The
original deadline and earlier exceptions remain recorded.

Agent repairs cannot ask Harry for reviewer approval. The native recovery API
only accepts an unclaimed blocked operator repair, preserves declared candidate
artifacts atomically, and requires a different named reviewer. It cannot reclassify
a genuine human choice. Original repair holds are reconciled from durable events
if an implementer accidentally edits raw row flags; a relabel to needs_input does
not become a Harry responsibility. Implementer-written verification reports are
candidate evidence until another profile reproduces the requested outcome.

Only the explicitly audited genuine cohort can recover historical completed
requests missing a substantive result or acceptance receipts. A blocked/triage
original additionally needs a declared agent fault bound to its exact latest
hold event, with source and diagnosis. A later hold invalidates that diagnosis;
existing human-only choices are preserved. Recovery hands existing work to a
different reviewer once, with a fixed 900-second deadline and safe native stop
or bounded repair on exhaustion. It never replays the original external action
or treats a legacy done flag as acceptance.

Repair context resolves its authenticated original through the durable native
repair-created event. That authority outranks implementer reports and erroneous
prior review instructions. A native completion contract independently checks the
original dependency hold, absence of an execution claim, and no hold drift during
the candidate cycle. Relabeling the original needs_input cannot pass with a list
of claimed SQL tests: native completion sends it back for bounded rework. The
contract is generated by the runtime, rechecked under the completion transaction,
and bound to the original and fault event before reconciliation can resume work.
These checks prove transition integrity; they do not substitute for independent
functional reproduction of the original requested outcome.

Explicit Harry confirmation/testing claims cannot use this task's AGENT question
answers as human proof. The native gate requires the exact tagged HARRY question
receipt instead of a stored answer, response duration, or unrelated human reply;
it returns the observed false-claim candidate to its implementer. This addresses
that concrete provenance counterexample, not general semantic truth in prose.
Human acceptance of a change and receipt of a particular delivered result remain
separate; neither gate synthesizes a result-reply receipt.

An invalid already-completed audited answer gets at most one corrective review
by another installed profile, with a fresh fixed 900-second review phase. The
original deadline and old answer evidence remain recorded. Older review deadlines
cannot accidentally terminate this corrective phase; no external action is replayed.

Optional board policy `release_entrypoint: {home, path}` declares the one owned
Jarvis service entry. Existing dispatcher reconciliation restores it from the
selected immutable controller only after seal verification and preflight, under
the same selector lock. It saves displaced bytes before atomic replacement and
checks readback. A fixed 120-second repair deadline, three attempts and at most
three restorations per hour prevent endless file fights. Invalid seals/preflight
never overwrite the entry, and an entry fault cannot abort queue reconciliation.
This does not restart processes or install another supervisor or watcher.

Original-request expiry safely stops active work or parks idle work before another
external action, retaining an unproved stop fence. One existing bounded repair
owns recovery. Explicit corrective acceptance phases retain their separate fixed
deadline and cannot renew original execution time. Native repair blocks cannot
wait on their own verification descendants; existing exact circular holds get
one corrective review handoff. They cannot manufacture another Harry question
or a chain of verification cards to bypass owned review.

A verified repair cannot repeatedly renew overdue original execution. It may
hand its existing evidence to one bounded original acceptance review through a
native API guarded by actual repair completion, independent review and machine
contract provenance. This phase forbids external-action replay. Rejection cannot
restart expired implementation; the original stays an agent-owned exception.

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


2026-10-01 audited outcome and terminal corrections:

- Deadline and repair-stop receipts fence the central native claim transaction even if queue promotion makes a task ready again. Only a later bounded acceptance handoff may claim review; original effects cannot replay.
- Genuine originals and operator repairs cannot archive around acceptance. Archival requires owned independent completion and, for an original, matching result delivery. Human receipt remains separate and is not required or inferred.
- An audited cohort request archived after invalidation is restored to one bounded independent acceptance phase. Its old result and archive reason remain evidence. The deadline is fixed and cannot renew through reconciliation.
- Native completion context includes bounded declared-request and live-platform facts. A positive overall health claim contradicted by those facts returns to rework. Exact independently audited bad-completion receipts can trigger one corrective review without reclassifying later human choices.
- These gates prevent observed false claims and state escapes. They do not establish general semantic truth or capability parity; real outcome probes and same-thread readback are still mandatory.

An exact historical repair fault may receive one bounded ownership correction review. Its generated native receipt is explicitly limited to authority; the independent reviewer still owes reproduced functional evidence. The audit must match the latest hold/rejection, preserve the original dependency fence, and cannot authorize another implementation run.


R5 real-request counterexamples corrected in R6:

- A formal original -> repair edge made an agent repair wait for the failed original, which was itself waiting for that repair. Reconciliation reverses only that exact edge when durable repair provenance, authenticated origin, original dependency hold, and absent claims agree. Other parents remain intact. A remaining cycle rolls back and retains the agent exception.
- Historical terminal review after a rejected candidate needs genuinely new evidence. The handoff includes native invalidation/terminal event receipts, explicitly limited to failure provenance. It neither renews the phase nor proves the requested functional outcome.
- Controlled drills include unrelated-parent preservation, transactional cycle rollback, unchanged evidence after rejection, and one bounded terminal handoff. No live R5 intervention occurred during the fixed trial while these inactive corrections were built.


Additional R5 observed evidence failures:

- Native review rejects the observed demand to change an agent-owned original from dependency to needs_input. The rejected predicate stays an agent exception; it does not spend another rework attempt or authorize live SQL ownership mutation. Actual functional rejection remains available.
- Declared authenticated live-state questions use independent native ledger/platform reads in the completion transaction. Their canonical answer names unresolved requests, owners, unavailable/unproved platforms, audit scope, and unverified older cards. Caller narration is superseded with a recorded hash; native factual receipts do not claim repairs are complete or infer human receipt. An owned different-profile review is still required.
- An invalidated result whose corrective acceptance exhausted its fixed phase gets at most one final terminal correction, using new failure evidence. It cannot restart original execution or renew that final phase through reconciliation.

Native factual status acceptance follows durable original -> repair provenance, so restricted shell query failures can be repaired by an owned independent review using the native reader. Goal-judge blocks with a verified circular sign-off descendant are agent-owned even when the block lacks a dependency label. The original remains held until native accepted repair provenance permits its final bounded acceptance.

Corrective repair review preserves the original implementation owner rather than assigning deployment to a prior reviewer. A genuine rejection can rework that repair inside the one fixed corrective phase, capped by its phase/rework deadlines and review budget. A later stop fences it again; originals with stop receipts never gain execution permission. Historical failure records remain intact, while the explicit bounded corrective phase has its own nonrenewing review counter.

An invalidated authenticated request that lost its notification subscription regains the original platform/chat/thread/user and reply-to message from durable origin/transport evidence. Restoration starts both text and artifact cursors at current history, so the old false result and stale notices do not replay. The event records route restoration, never successful delivery or human receipt. Missing non-Discord route proof stays an agent exception.

When an acceptance invalidation supersedes the last completion, reconciliation withdraws that stale canonical result atomically. Its exact previous text, hash, completion ID and invalidation ID remain in a durable event. A later valid completion is never cleared by a stale invalidation.

Accepted repairs now carry their new completion event, owned review provenance, native original contract and actual acceptance receipts into the original final review. This changes evidence after a prior rejection without replaying original actions or renewing its execution deadline.

A blocked reviewer who calls recorded native ownership restoration a kernel SQL rollback is returned to the original implementer once, only when that causal guard event belongs to this review attempt and the original still has its unclaimed agent-owned hold. An explicit finite correction phase preserves all old run/stop/failure records. Its source is native fact correction, not an inferred reviewer or Harry verdict. Missing guard provenance, genuine unrelated prerequisites or later ownership changes refuse recovery. Further failures cannot renew it.

Goal judges now receive structured acceptance receipts and the durable authenticated original repair authority. Old candidate task flags cannot replace the requested outcome, and owned goal failures never instruct Harry to solve deployment or tool metadata. This does not weaken native completion checks.

Independently audited distinct new faults can receive correction only when the exact new fault follows the previous handoff, has not been audited before, and the lifetime ownership-correction count remains below max_audited_repair_faults (default 3). Each phase has its own fixed deadline; stale or repeated audits cannot renew it.

The real R7 health chain had accepted native factual repair -> extra unclaimed internal sign-off -> original question. For the explicitly declared authenticated factual status mode only, its accepted repair can replace that extra machine-created sign-off edge with the mandatory owned original final review. The sign-off is not marked passed/completed; its history and all unrelated/Harry/claimed prerequisites remain intact. Original execution never becomes ready between edge correction and final review. Generic functional requests retain their verification dependencies.

Goal judging of a declared factual status question uses the original recorded request and current native facts, not a candidate demand that everything be healthy. Completion recomputes the factual answer atomically. A truthful negative status satisfies the question; it does not certify unrelated functional repairs or infer human receipt.

Exception reconciliation deduplicates identical cause payloads under its transaction while preserving changed causes, so a fixed evidence gate followed by a distinct dependency failure does not remain mislabeled as the old error. Malformed older event payloads are retained safely.

The shared worker goal builder now inherits repair authority from the durable original request. Owned reviewers must reproduce the result or return concrete failed probes to the implementer; rejection is an agent step, not a Harry-only choice. Completion still requires independently accepted original outcome evidence.

An explicitly audited terminal repair deadline is eligible only as the latest exact fault, with original authority, no claim, a new source event and remaining lifetime correction budget. A corrective reviewer differs from the preserved implementer; the previous reviewer may retry corrected native logic without inheriting implementation. Rework timers apply within their correction phase. An expired earlier timer cannot stop the new phase, and current phase/rework deadlines still prevent further execution.

The actual worker goal-loop failure callback now routes an owned repair review through native request_changes instead of blocking the reviewer as though it were the implementer. This requires the exact current run, declared original cohort, valid original repair hold and recorded review claim. Stale runs, implementation workers, unrelated work and human holds retain the ordinary block path. Rework remains within the current fixed phase and cycle limit.

Worker context and judging receive the same current owned goal. Explicit safe release authority is supplied only for the declared request cohort with recorded human authorization; it requires isolated source, mandatory preparation and actual serving/rollback checks, preserving old Jarvis and original unfinished work. Missing deployment or live evidence must be performed or rejected for agent rework, not recast as a Harry-only authorization choice.

Question acceptance checks the existing authenticated bridge author contract as well as the exact question marker. A pilot/reviewer writing HARRY[qid] does not authenticate the answer. The observed pilot relabel and its original AGENT answer remain historical evidence; controlled red/green probes reproduce the guard bypass in isolation. This does not infer a human result receipt or certify all narrative truth. Current real human acceptance remains unobserved.

Exact audited recovery can start a new candidate after a historical native restoration of this same repair's original hold. The old restoration event is retained and cited, current ownership must be intact and unclaimed, and the actual new review creates a fresh candidate boundary. Unknown/wrong-repair restorations and current human holds refuse it. Ordinary completion still rejects drift after that new boundary. A new typed native Harry-only choice after repair creation is preserved unless its exact event was independently diagnosed as an agent fault; raw row relabels cannot manufacture a choice.

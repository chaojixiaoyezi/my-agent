# Continuity follow-up after the Opus v2 review

The v2 review covered commit `922510c09`. The intervening session-space patch ended at `658bd3abf`. Use the latter as the base when reviewing this follow-up.

## Reply delivery

The host records a reply obligation when a typed direct user input reaches the provider acknowledgement path. A Goal update or plugin interjection cannot create one. A failed model attempt and a compact yield can lose their in-memory prompt state while the obligation survives. Retry reads the original input identity and committed consumption receipt. The public outbox freezes the inputs covered by the response. Settlement follows the delivery commit. Cancellation, private application of guidance and the audit message-tool contract retain their original behavior. An authorized send-message receipt can fulfill the external send without sending the final again.

## Transcript and navigation

The history ledger distinguishes the durable repair queue from its reconstructible index of canonical messages. The SQLite file holds both. Keep it with the conversation store during an upgrade. Legacy JSON repair records migrate in bounded pages. Incomplete index coverage defers a repair. Completed answers take precedence over older failed attempts in both the history display and text-only model input. Read-only business and watch pages retain arrow navigation and leave the execution queue alone. On the main composer the up arrow can recover editable input.

## Continuing a task

A host-issued cutoff receipt identifies the user-origin task, thread, input, attempt, reason and sequence. Only the four implemented technical cutoff reasons can issue one. A normal final does not schedule another slice. A qualifying Goal or ordinary task can continue on its existing scheduler with a limit of three consecutive continuations and a material-progress check. New user input, cancellation and Goal termination invalidate stale authority. A child event can reset the streak. The existing claim lease handles recovery after a claimed execution crashes.

The wake is published while holding the task guard before the matching receipt commits. A consumer takes the same guard. A process death before the receipt commits leaves an uncommitted closeout which requires ordinary request recovery. A wake alone cannot issue execution authority. A legacy task without a trusted user origin cannot acquire this authority retrospectively.

## Review prompt

Review the changes after the session-space commit against the v2 findings F1–F5 and risks R1–R5. Inspect one failure after provider acknowledgement, one compact yield, one delivery crash and one retry with a completed final outside the old history window. Check a read-only listing with queued input. For continuation, trace a foreground user request through closeout, publication, claim, child waiting, cancellation and visible completion. Check bounds on history migration and input obligation recovery for a long-lived owner. Report each confirmed defect with the file, trigger, actual result, expected result and a focused regression. Distinguish unit, real scheduler, real TUI and actual external delivery evidence. Review the current source before rerunning any tests.

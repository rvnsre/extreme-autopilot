# v0.2.0 correctness and recovery audit

Baseline: `424a7dae21baada152006eeb7aca802ac2568bf1` (v0.1.0 preview).

The initial regression run reproduced **14 failing cases** against that baseline. They covered the defects below; multiple tool/number cases exercise the same underlying defect. These are executable regressions in `tests/test_audit.py`, not a claim that arbitrary autonomous tasks are now error-free.

| Defect reproduced | Resulting correction |
|---|---|
| `navigate` bypassed authority checks for file writes, processes and desktop typing | Every tool now has an explicit allowed intent; input/mutation needs an original-brief authorization quote. |
| A write could succeed and its readback fail, yet the receipt said the outcome was known | Input/commit boundaries are tracked. Failures after input are `unknown`; validation failures before input remain known rejections. |
| The step limit terminated a job before its last operation/result could be verified | Outstanding work settles first, and a final verification is still permitted at the limit. |
| History silently lost receipts after 2,000 actions | Full durable history remains available; only model context is compacted. |
| Duplicate acknowledgements depended on JSON key ordering | Canonical comparison ignores object order while preserving JSON value types. |
| Late billing settlement left cancelled reports with stale totals | Report billing, pending operations and late receipts refresh transactionally. Recovery updates these totals too. |
| Negative/NaN reservations could enter the ledger | Reservations/settlements and configuration prices reject invalid or nonfinite numbers. |
| Earlier screenshots were presented as current context | Only a screenshot from the latest result is attached as current visual input. |
| A failed cloud call could prevent otherwise feasible local planning | Cloud-preferred planning falls back to text-only local planning. Independent final cloud verification remains required. |
| Changing arguments could evade the fence on an uncertain mutation | Any unresolved uncertain operation fences all further state changes for that job; read-only work and partial reporting remain possible. |
| Windows children could execute before job assignment failed | The primary thread is created suspended, assigned to its job and only then resumed. The regression deliberately delays/rejects assignment and verifies that the child's marker file is never written. |

Additional hardening has regression coverage in `tests/test_hardening.py`, the existing suites, real-browser CI and frontend tests:

- One active worker session leases the queue. A second session is rejected for 45 seconds after the owner's last contact. A takeover sees redelivery, never blind replay. Workers recheck task cancellation/deadline before input.
- Protocol 2 is mandatory for v0.2 workers. Certificate/setup and execution cleanup always release worker/coordinator locks, including failure paths.
- Submission retries use an `Idempotency-Key`; concurrent retries return one job, and changed payloads with the same key are rejected. The panel and CLI send keys automatically.
- Schema initialization/migration is serialized. Upgrading a running v0.1 coordinator is refused before altering its database; concurrent offline migration preserves existing jobs.
- Browser freshness includes keyboard focus. Noneditable/disabled targets are rejected before input. Coordinate input retains its visual freshness checks.
- Process stdout captures advertise truncation at the actual 64 KB capture boundary. Ordinary child processes are stopped after their parent exits; pipe draining and all process resources are bounded and cleaned up.
- Model deadlines/cancellation are checked between providers and between local metadata probing and generation. Valid local model names containing `cloud` are no longer misclassified solely by substring.
- Receipts require strict types, finite JSON and valid artifact base64/size/SHA-256. Ambiguous authentication headers are rejected. Protected responses are not cached; the panel has CSP and MIME-sniffing protection. Configuration validation hides secret inputs.
- The panel's polling is serial, abortable and discarded when its view changes. Stale polls cannot overwrite cancellation or a newly selected job. A stopped scheduler produces HTTP 503 instead of a healthy result.

The Windows implementation follows [CREATE_SUSPENDED](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags) and [job-object lifecycle semantics](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects). Jobs manage ordinary process descendants; they are not an OS security sandbox. The dedicated VM remains mandatory.

## Verification limits

See `VALIDATION.md` for exact results and CI links. Unit fixtures do not prove model judgment or authorization semantics. Live Ollama/OpenAI, the user's Windows VM, unattended GUI reliability and full 24-hour acceptance remain unverified. This is an audited implementation preview, not a production certification or a zero-error guarantee.

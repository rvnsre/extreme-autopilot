# Trust and operating boundary

The coordinator has the OpenAI key and local model connection. The VM has a separate bearer token for worker routes. The API token cannot be used on worker routes and the worker token cannot read coordinator API routes. User-facing APIs require bearer authentication before buffering bodies; browser origins are restricted, request bodies are capped and worker transport verifies TLS.

The worker executes arbitrary task-generated code as its OS user. Filtering process environment variables avoids accidental inheritance of EA/API secrets; it does not stop malicious code from inspecting the worker's processes, filesystem or network. **A dedicated disposable VM, separate credentials and no host shares are required.** Do not treat Python path checks, model instructions or `EA_ISOLATED_WORKER=true` as a sandbox or attestation of actual isolation.

Initial authorization quotes and explicit external destination URLs prevent some accidental scope expansion. They cannot reliably determine the meaning of every click, key press, generated program or indirect instruction. Prompt injection, exfiltration through generated code and semantic misclassification remain risks. Do not give the VM production secrets, financial access, sensitive personal records or privileges whose misuse would be unacceptable. v0.1 has not been penetration-tested or certified for unrestricted unattended production use.

Browser private-address literal/local-name checks are application-level checks, not DNS-rebinding protection or a network firewall. Enforce VM network isolation separately. Generic browser/network/process tools can communicate with the Internet as the VM user.

Job prompts, UI observations, screenshots, model responses selected for execution, reports, files and event logs may contain private information. They stay in configured data directories but portions are sent to OpenAI during escalation and final verification. No anonymization guarantee is provided. Keep data directories and `.env` files outside Git. Retention is operator-controlled in v0.1; no automatic deletion is performed.

File captures are authenticated, served as downloads and never embedded as HTML. React renders model/task text as text. The panel does not use third-party scripts or store its bearer token in persistent browser storage. Avoid logging request bodies or Authorization headers in proxies.

Crash semantics: worker journals before execution and caches before acknowledgement. A crash between those records produces **unknown**, not automatic replay. Identical uncertain operations are fenced, but this is not exactly-once delivery for arbitrary external services. Use application receipts or service idempotency keys when available. External actions can complete after a request timeout or cancellation.

Report vulnerabilities privately to the repository owner using an available private contact channel. Do not publish private logs, API keys, screenshots or exploit data in public issues.

# Verification and acceptance status

This is a source implementation preview. It must not be represented as a zero-error or production-accepted autonomous agent.

## Local verification

Windows / Python 3.11.9 verification on 2026-11-10:

- **63 Python tests passed**, one opt-in real-browser test skipped; measured line coverage **71%**. Desktop tools and live model quality are not covered by that percentage. One upstream Starlette/httpx deprecation warning remains.
- Ruff checks passed. Frontend TypeScript/Vite production build and **4 frontend tests** passed.
- Real filesystem/process integration passed through the queue and worker protocol; model decisions in this test are synthetic fixtures.
- Reservation concurrency, lost billing response, cancellation, unknown command redelivery, independent verification rejection, long-task evidence retention and Windows journal handle release have regression coverage.
- Version-pinned dependencies pass `pip check`. Built wheel contains the panel's HTML/CSS/JS assets.
- Full npm dependency audit reports zero known vulnerabilities after updating the test runner to Vitest 5.1.0; both production and development dependencies were included.
- A **72-second synthetic soak** completed **422 cycles with zero failures**, including duplicate acknowledgements and journal/store reopen. It is not the 24-hour acceptance run.
- The local Edge browser test could not launch successfully inside the execution sandbox. Bundled Chromium installation also encountered a host `realpath` permission error. The opt-in real-browser test is supplied in a separate Windows CI job; its result must be checked independently.
- Panel visual QA could not complete: Computer Use stopped because it could not reliably determine the current Windows browser URL. No visual-QA pass is claimed.

## Required live acceptance (not yet completed)

No OpenAI key, configured local Ollama model or dedicated Windows VM was supplied to this build environment. Consequently, actual model quality, full desktop execution and VM reboot recovery are not verified here. Do not count mocked or synthetic model responses as live acceptance.

1. Configure the host and VM using docs/SETUP.md; keep task-only accounts and data outside Git.
2. Confirm `doctor --models` and actual structured-generation/vision calls work with the selected models.
3. Run all three example briefs using the real models and dedicated worker. Inspect downloaded artifacts and original application state independently.
4. Interrupt before dispatch, after journal-before-input, after external submission and after result-before-ack. Verify receipts/independent observations and confirm no blind mutation replay.
5. Exercise cloud timeout, invalid JSON, budget exhaustion, process timeout, locked desktop, expired login, CAPTCHA and cancellation.
6. Run `python scripts/soak.py --hours 24 --out C:/AutopilotData/soak.json`. In addition to that synthetic storage test, run an attended initial 24-hour VM/model workload before unattended use. Record elapsed wall time, failures, duplicates, cost and resource growth.
7. Release only after reviewing every failure and recording successful live acceptance. A green unit-test CI run alone does not satisfy these criteria.

## Known limits

- A model verifier can still make semantic mistakes; prompt-injection resistance is not proven.
- Code runs with VM-user privileges; path validation is not an OS sandbox.
- Unknown outcomes are conservatively fenced, not magically made exactly-once.
- Screenshot freshness hashes cover bounded text/accessibility state; purely visual changes can still occur. Coordinate actions need recent visual observation and remain susceptible to UI races.
- A dedicated unlocked interactive session is required. The worker cannot recover from a locked Windows login screen by itself.
- Large artifact export (>512 KB per captured file) and parallel desktop workers are outside v0.1.
- Price configuration is operator-maintained. Budget accounting is not a hard provider billing cap.

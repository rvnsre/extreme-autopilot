# Verification and acceptance status

This is a source implementation preview. It must not be represented as a zero-error or production-accepted autonomous agent.

## v0.4.0 economy verification

Windows/Python 3.11 local verification: **155 Python tests passed**, 3 platform/opt-in tests skipped, one upstream Starlette/httpx warning; backend line coverage **75%**, economy module **90%**. Ruff/formatting, `pip check`, 6 frontend tests and TypeScript/Vite build passed. The 22 direct economy integrations are pinned in package metadata and the complete portable lock file. Installing the built wheel into a separate target verified its package version, bundled panel and all 22 library integrations.

[v0.4.0 CI run 42788754656](https://github.com/rvnsre/extreme-autopilot/actions/runs/42788754656) passed **all 10 jobs** for code commit `060d3ac6a28b6f54d0e48037d7ed0a07df9c954c`: all six Windows/Linux/macOS × Python 3.11/3.12 jobs installed and imported the 22 integrations, passed dependency checks, backend/frontend tests, full npm audit (zero findings) and builds. Windows/macOS each passed 155 Python tests (3 skipped), Linux 154 (4 skipped); the differences are platform/opt-in tests. Real Chromium passed on all three OSes, and real X11 Unicode input/readback passed separately. Measured backend coverage was 75% Windows, 73% macOS and 72% Linux. This verifies synthetic execution and the optional economy integration, not real model quality or production acceptance.

After explicit tokenizer warmup, four synthetic fixtures measured **58.59% combined estimated context-token reduction** using `cl100k_base` (10,090 → 4,178 estimated tokens). Individual reductions were 55.63%, 54.22%, 75.23% and 35.67%; processing plus the final-evidence preservation check took approximately 13–210 ms per case on this machine. These are synthetic text-context estimates, not live-task accuracy, provider dollars saved or exact token counts for arbitrary models. No model request was made. See [benchmark JSON](docs/benchmarks/economy-v040.json) and [implementation/38 changes](docs/ECONOMY.md).

Regression tests cover exact job/model/context cache isolation, concurrent plan coalescing and one billing settlement, no action/verdict cache reuse, cancellation before reuse, cooldown deferral without a false failure, corruption/decompression/expiry guards, unchanged authority/receipt IDs including large integers, non-lossy final verification, HTML/table/code extraction and private authenticated diagnostics. Real JPEG fixtures verify separate text/image transport limits, exclusion of image base64 from text estimates and pre-charge rejection of invalid images. Low cloud ceilings do not reduce the local model's configured capacity. Live model/VM, native macOS GUI and full 24-hour acceptance remain pending.

## v0.3.0 platform verification

On Windows/Python 3.11, **118 tests passed**, 3 platform/opt-in tests skipped and one upstream Starlette/httpx warning remained. Ruff and formatting checks passed; 6 frontend tests and the production build passed.

[Three-OS CI run 42728622135](https://github.com/rvnsre/extreme-autopilot/actions/runs/42728622135) passed **all 10 jobs** for source commit `4abb7a24a363fbed566a9cdc10d0c102a2884df5`:

- Windows/Linux/macOS × Python 3.11/3.12: backend tests, 6 frontend tests, full npm audit (zero findings), type checks and wheel/source builds passed. Windows and macOS ran 118 Python tests each; Linux ran 117. Platform-specific and opt-in skips were 3/3/4 respectively. macOS includes the native permission preflight, not native GUI input acceptance.
- Real Chromium passed on **all three OSes**. Linux used a runner-specific AppArmor profile to keep Chromium sandboxing enabled.
- Real X11/Openbox integration passed Unicode Azerbaijani/Russian typing into a disposable application's window, screenshot, keyboard submission, independent file readback and stale-frame rejection.
- CI exposed a Darwin process-group cleanup failure: `killpg` can report EPERM for zombie-only orphan groups. Cleanup now verifies the group has no live members before suppressing denial; permission failures with live members remain errors. Output threads are joined/stopped even if cleanup fails. Both macOS Python versions passed the real child-process lifecycle regression after this correction.

[PLATFORMS.md](docs/PLATFORMS.md) describes supported backends, required permissions and native Wayland restrictions. macOS native GUI input requires dedicated-worker acceptance; permission preflight alone is not a GUI input pass. Live model/VM and full 24-hour acceptance below remain pending.

The detailed results below are retained v0.2.0 evidence unless explicitly stated otherwise.

## Local verification

Windows / Python 3.11.9 verification on 2026-11-10:

- **104 Python tests passed**, one opt-in real-browser test skipped; measured line coverage **74%**. Desktop tools and live model quality are not covered by that percentage. One upstream Starlette/httpx deprecation warning remains.
- Ruff checks passed. Frontend TypeScript/Vite production build, Prettier checks and **6 frontend tests** passed.
- The initial audit reproduced **14 failing cases** on v0.1.0; their regression tests now pass. See [AUDIT.md](AUDIT.md) for defects, fixes and remaining limits.
- Real filesystem/process integration passed through the queue and worker protocol; model decisions in this test are synthetic fixtures.
- Reservation concurrency, lost billing response, cancellation, unknown command redelivery, independent verification rejection, long-task evidence retention and Windows journal handle release have regression coverage.
- Version-pinned dependencies pass `pip check`. Built wheel contains the panel's HTML/CSS/JS assets.
- Full npm dependency audit reports zero known vulnerabilities after updating the test runner to Vitest 5.1.0; both production and development dependencies were included.
- Installing the built wheel into a separate target and importing the installed package/panel assets passed.
- A **122.4-second synthetic soak** completed **688 cycles with zero failures**, including duplicate acknowledgements and journal/store reopen. It is not the 24-hour acceptance run.
- The local Edge browser test could not launch successfully inside the execution sandbox. Bundled Chromium installation also encountered a host `realpath` permission error. The opt-in real-browser test is supplied in a separate Windows CI job; its result must be checked independently.
- Panel visual QA could not complete: Computer Use stopped because it could not reliably determine the current Windows browser URL. No visual-QA pass is claimed.

## GitHub Actions verification

[v0.2.0 quality checks run 42726783605](https://github.com/rvnsre/extreme-autopilot/actions/runs/42726783605) completed successfully for source commit `96aa005b1755be3d9203a9a06753496338489bbe`.

- All four Windows/Linux × Python 3.11/3.12 jobs passed backend tests, frontend tests, formatting checks and package builds.
- Windows jobs passed **104 Python tests** with one opt-in browser skip; Linux jobs passed **103** with two skips (the additional test requires the Windows process API). Each matrix job passed **6 frontend tests** and the full npm audit with **zero findings**. Measured backend line coverage was 74% on Windows and 70% on Linux.
- The separate Windows real-Chromium job passed `test_real_browser_elements_keyboard_freshness_and_screen` (**1 passed**), exercising real page navigation, element filling/focus, keyboard activation, DOM/focus freshness rejection and JPEG observation.
- This verifies the browser executor with a synthetic website on a hosted runner. It does not verify real Ollama/OpenAI decisions, authenticated production websites or a user's dedicated VM.

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
- Large artifact export (>512 KB per captured file) and parallel desktop workers are outside v0.2.
- Price configuration is operator-maintained. Budget accounting is not a hard provider billing cap.

# v0.4.0: local token and cost engineering

The coordinator integrates **22 direct libraries** and the **38 changes** listed below. This is a bounded optimization layer, not a promise of error-free execution or a fixed percentage of provider savings. Local extraction/ranking needs CPU and disk space; workers do not install this extra. No extra LLM or embedding service is introduced.

## Install and operate

The coordinator installers now install `requirements-economy.lock.txt`. Existing installations should stop services, preserve private data/journals, install that lock alongside their platform's requirements, install the updated package and rebuild the panel. The basic `requirements-coordinator.lock.txt` remains available for a minimal installation. Missing optional imports are reported in diagnostics; the original model path remains available. Disable explicitly with `EA_ECONOMY_ENABLED=false` for an A/B comparison.

Settings:

```dotenv
EA_ECONOMY_ENABLED=true
EA_ECONOMY_TEXT_LIMIT=2400
EA_ECONOMY_PLAN_TTL=120
EA_ECONOMY_PLAN_TOKENS=1024
EA_ECONOMY_DECISION_TOKENS=1024
EA_ECONOMY_VERDICT_TOKENS=512
```

`EA_ECONOMY_TEXT_LIMIT` accepts 800–12,000 characters per observation excerpt. The complete annotated replacement must be smaller in UTF-8 bytes before it is used. This does not guarantee fewer tokens for every tokenizer.

Run these commands with the coordinator's private env file. On Windows use `.venv/Scripts/autopilot.exe`; on Linux/macOS use `.venv/bin/autopilot`:

```text
autopilot --env-file /private/coordinator.env economy status
autopilot --env-file /private/coordinator.env economy warm-tokenizer
autopilot --env-file /private/coordinator.env economy analyze --file report.html
autopilot --env-file /private/coordinator.env economy analyze --file data.json --query results.body
autopilot --env-file /private/coordinator.env economy benchmark --out /private/benchmark.json
```

Only `warm-tokenizer` intentionally downloads tokenizer assets during initial setup. Restart the coordinator after warming or replacing those assets. Runtime estimates use that local `cl100k_base` vocabulary or a clearly labeled UTF-8 byte upper bound. **cl100k estimates are not exact counts for arbitrary OpenAI or Ollama models.** No estimator reduces the conservative byte-based cloud reservation or relaxes local context gates. Storage compression is reported separately; compressed bytes are never called model token savings.

## 22 integrated libraries

These are direct integrations, not transitive-package counts. Their installed versions and roles appear in `economy status` and authenticated `GET /api/economy`. Exact portable dependency closures are committed in the lock file.

| Library | Actual use | Primary source |
|---|---|---|
| orjson | Stable compact UTF-8 request/cache JSON | [project](https://github.com/ijl/orjson) |
| tiktoken | Explicitly warmed, offline token estimates | [project](https://github.com/openai/tiktoken) |
| cachetools | Bounded token LRU, schema TTL and cooldown maps | [docs](https://cachetools.readthedocs.io/) |
| diskcache | Job-scoped expiring plans and bounded diagnostics | [docs](https://grantjenks.com/docs/diskcache/) |
| xxhash | Exact-paragraph fingerprints, with equality confirmation | [project](https://github.com/ifduyue/python-xxhash) |
| zstandard | Bounded local cache compression | [project](https://github.com/indygreg/python-zstandard) |
| blake3 | Request identity and cache payload integrity | [project](https://github.com/oconnor663/blake3-py) |
| regex | Unicode segmentation and script detection | [project](https://github.com/mrabarnett/mrab-regex) |
| ftfy | Observation-only text cleanup | [docs](https://ftfy.readthedocs.io/) |
| beautifulsoup4 | Noise removal and source-link collection | [docs](https://www.crummy.com/software/BeautifulSoup/bs4/doc/) |
| lxml | Local HTML parsing and extraction fallback | [docs](https://lxml.de/) |
| trafilatura | Local article-body extraction | [docs](https://trafilatura.readthedocs.io/) |
| markdownify | Preserve HTML tables/code as readable Markdown | [project](https://github.com/matthewwithanm/python-markdownify) |
| rapidfuzz | Near-duplicate diagnostics; fuzzy text is retained | [docs](https://rapidfuzz.github.io/RapidFuzz/) |
| datasketch | MinHash similarity diagnostics | [docs](https://ekzhu.com/datasketch/) |
| rank-bm25 | Local goal-relevance ranking | [project](https://github.com/dorianbrown/rank_bm25) |
| snowballstemmer | English/Russian lexical stemming for ranking | [project](https://github.com/snowballstem/snowball) |
| msgspec | Typed cache envelopes | [docs](https://jcristharif.com/msgspec/) |
| jsonschema | Independent validation before cached-plan reuse | [docs](https://python-jsonschema.readthedocs.io/) |
| jsonpatch | Before/after numeric-manifest audit deltas | [project](https://github.com/stefankoegl/python-json-patch) |
| jmespath | Offline JSON document projection | [docs](https://jmespath.org/) |
| boltons | Bounded grouping of long observation segments | [docs](https://boltons.readthedocs.io/) |

## 38 implemented changes

1. Optional coordinator-only economy mode with an explicit off switch.
2. Canonical compact Unicode JSON, rejecting nonfinite values instead of silently changing them.
3. Bounded schema TTL cache to avoid rebuilding the same schema.
4. Planning uses tool names instead of full argument schemas; execution decisions retain argument constraints with redundant title annotations removed.
5. Local HTML script/style/navigation/footer removal.
6. Article-body extraction without another model request.
7. Table/code-aware Markdown extraction.
8. Bounded HTTP(S) source-link collection; links remain untrusted data.
9. Observation-only Unicode repair and horizontal whitespace normalization.
10. Unicode-aware paragraph/sentence chunking with bounded work.
11. Exact duplicate paragraph removal with a hash-plus-equality check.
12. Fuzzy/MinHash diagnostics that retain near matches instead of discarding changed facts.
13. English/Russian stemming for lexical retrieval; Azerbaijani source text is retained, not translated.
14. BM25 selection of chunks relevant to the original brief.
15. Beginning/end retention alongside ranked excerpts.
16. Original instructions, action bodies, authority quotes and receipt metadata stay unchanged.
17. Annotated excerpts with input-text hashes, lengths, selected chunk IDs and provenance.
18. A replacement is used only if its complete annotated representation is smaller in UTF-8 bytes.
19. Independent final verification bypasses the lossy reducer and result cache.
20. Explicit one-time tokenizer warmup followed by network-free estimation.
21. Labeled UTF-8 upper-bound fallback when tokenizer assets are absent.
22. Bounded token-count LRU; image base64 is excluded from text-token estimates and transported under its own size/format guard.
23. Exact plan caching isolated by job, route/model, full context, system instruction and schema.
24. Concurrent identical planning calls share one in-flight generation.
25. Typed, versioned cache envelopes, integrity hashes and independent schema validation.
26. Cache TTL, 16 MiB storage limit and bounded decompression.
27. Zstandard local storage compression, explicitly separated from token savings.
28. Corrupt, invalid, oversized and expired cached plans are rejected.
29. A bounded 15-second cloud failure cooldown avoids immediate repeated paid attempts.
30. A cooldown defers work within the existing deadline instead of counting as a failed tool attempt.
31. Configurable stage-specific output ceilings: defaults of 1,024 tokens for plans/decisions and 512 for verdicts, bounded by the operator's overall ceiling. Truncated responses still fail validation.
32. Provider-reported cached/input/output token counters alongside conservative billing.
33. Authenticated library/configuration diagnostics API.
34. Authenticated per-job context and cache statistics API.
35. CLI local-document analysis with optional bounded JMESPath projection.
36. Reproducible four-case synthetic benchmark with no model requests or dollar-saving claims.
37. Counts/hashes-only optimization manifests, numeric JSON patches and retention bounded to the last 100 measurements for 30 days.
38. Azerbaijani panel counters for measured context reduction, plan-cache hits and provider-reported cached tokens.

## Trust, caching and cost limits

Only `Plan` responses can be cached. Action decisions, tool effects, screenshots and final verification verdicts are **never** reused from this cache. Cancellation/deadline checks happen before a cache hit and again after waiting for an in-flight plan. A cached plan still passes the normal engine/worker authorization and execution guards. No semantic cache or cross-job reuse is allowed.

Only observation `text` fields in recent action results are reduced. Model inputs contain explicit excerpt/provenance markers. The original durable receipts/artifacts stay in the coordinator database. The final verifier receives the existing engine's bounded evidence packet unchanged by this reducer; it does not suddenly receive every byte of an arbitrarily large source. A source excerpt can omit relevant facts, and a model can still reason incorrectly. This is why live task-quality evaluation remains required.

The static system/schema prefix is serialized consistently. Provider cache hits are observed from usage, not guaranteed. No new retention/cache-write request option is enabled. Current provider caching and billing behavior depends on the selected model; consult [official OpenAI prompt-caching documentation](https://developers.openai.com/api/docs/guides/prompt-caching). Reported cached input is charged at the operator's ordinary input rate in the conservative application ledger; this may overestimate a discounted provider bill. It does not account for every provider-specific surcharge and is not a hard billing cap.

Context token counters describe **text only**. Images use the existing separate configured image reserve and provider-reported usage. A valid JPEG up to 256,000 bytes is not incorrectly rejected merely because its base64 exceeds the 180 KB text-context limit. Bad base64, oversized payloads or missing JPEG transport markers are rejected before a paid call; transport validation does not certify image content or model vision quality.

An HTTP timeout is not automatically retried. Existing unknown billing reservations remain conservative. The engine may attempt a new planning/generation step after cooldown within the task's original budget/deadline; the cooldown is not an exactly-once provider billing guarantee. Output ceilings can cause some complex outputs to truncate; these are rejected rather than accepted as partial JSON. Disable economy mode or raise the corresponding `EA_ECONOMY_*_TOKENS` setting (and `EA_CLOUD_OUTPUT_TOKENS` ceiling) if your acceptance tests require longer structured outputs. Stage settings accept 256–16,384 tokens; local generation retains its existing 2,048-token maximum.

Metrics store counts/hashes, not source text. Cached plans can contain private goal descriptions; keep the economy data directory private and outside Git, like the existing task store. DiskCache's 16 MiB limit is a cache target, not a strict filesystem quota for database/WAL overhead. Cached-plan corruption does not create executable commands. Cache write/metric failures do not invalidate a successful provider result.

## Evidence

See [VALIDATION.md](../VALIDATION.md) for measured results and CI. A synthetic repeated-text percentage is not an accuracy score, a production workload forecast or measured provider dollar savings. All new libraries are exercised by local extraction/cache/model-boundary tests and the three-OS CI matrix; live OpenAI/Ollama/VM and 24-hour acceptance remain separate.

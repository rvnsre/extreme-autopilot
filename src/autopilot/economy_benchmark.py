"""Deterministic synthetic measurements; no model decisions, paid requests or quality claims."""

import time

from autopilot.economy import canonical
from autopilot.executor import tool_catalog


def benchmark(economy):
    cases = {
        "repeated_research": "\n\n".join(
            ["A local virtual environment keeps Python dependencies separate from other projects. " * 3] * 24
        ),
        "html_article": "<html><nav>Home Products Contact</nav><article><h1>Project checks</h1>"
        + "<p>Run the tests and inspect their exit codes before declaring success.</p>" * 50
        + "</article><script>tracking()</script></html>",
        "multilingual": "\n\n".join(
            ["Azərbaycan dilində hesabat hazırla. Русский текст для проверки. Keep source evidence. " * 5]
            * 20
        ),
        "unique_facts": "\n\n".join(
            f"Record {i}: measured value {i * 7}. Independent observations must be read back."
            for i in range(70)
        ),
    }
    results = []
    for name, text in cases.items():
        packet = {
            "original_brief": "Research the requested topic; retain sources and test evidence.",
            "recent_actions": [
                {
                    "id": "synthetic-source",
                    "body": {"tool": "http_get", "args": {}},
                    "result": {"ok": True, "unknown": False, "data": {"text": text}},
                }
            ],
            "tools": tool_catalog(),
        }
        started = time.perf_counter()
        prepared, report = economy.prepare(packet)
        final, final_report = economy.prepare(packet, final=True)
        assert final["recent_actions"] == packet["recent_actions"]
        assert prepared["original_brief"] == packet["original_brief"]
        report.update(
            case=name,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            final_evidence_unchanged=final_report["final_evidence_preserved"],
            estimated_reduction_percent=round(
                100 * (1 - report["after_tokens"] / report["before_tokens"]), 2
            ),
            no_model_request=True,
        )
        results.append(report)
    before = sum(row["before_tokens"] for row in results)
    after = sum(row["after_tokens"] for row in results)
    return {
        "synthetic_only": True,
        "live_quality_verified": False,
        "provider_dollars_saved": None,
        "cases": results,
        "combined_estimated_reduction_percent": round(100 * (1 - after / before), 2),
        "serialized_result_bytes": len(canonical(results).encode()),
    }

"""Optional local-only context engineering. Never a source of authority or completion proof."""

import base64
import hashlib
import importlib.metadata
import json
import logging
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import blake3
import cachetools
import diskcache
import ftfy
import jmespath
import jsonpatch
import jsonschema
import markdownify
import msgspec
import orjson
import regex
import snowballstemmer
import tiktoken
import trafilatura
import xxhash
import zstandard
from boltons.iterutils import chunked
from bs4 import BeautifulSoup
from datasketch import MinHash
from lxml import html
from rank_bm25 import BM25Okapi
from rapidfuzz.fuzz import ratio

LIBRARIES = {
    "orjson": "Canonical compact UTF-8 serialization",
    "tiktoken": "Offline BPE estimates after explicit tokenizer warmup",
    "cachetools": "Bounded schema/token memory caches",
    "diskcache": "Job-scoped expiring plan cache",
    "xxhash": "Fast exact paragraph fingerprints",
    "zstandard": "Bounded local cache compression",
    "blake3": "Cache envelope integrity and request identity",
    "regex": "Unicode segmentation and script-aware tokenization",
    "ftfy": "Observation-only Unicode cleanup",
    "beautifulsoup4": "HTML navigation/script removal and link discovery",
    "lxml": "Non-networking HTML parser and extraction fallback",
    "trafilatura": "Article-body extraction without an LLM",
    "markdownify": "Table/code-aware HTML-to-Markdown conversion",
    "rapidfuzz": "Near-duplicate diagnostics without deleting fuzzy matches",
    "datasketch": "MinHash similarity diagnostics",
    "rank-bm25": "Local relevance ranking of observation chunks",
    "snowballstemmer": "English/Russian lexical normalization",
    "msgspec": "Typed cache envelope encoding/validation",
    "jsonschema": "Independent cached-plan schema validation",
    "jsonpatch": "Auditable optimization manifest deltas",
    "jmespath": "Offline document/diagnostic projection",
    "boltons": "Bounded grouping of long paragraph segments",
}


def canonical(value):
    # orjson maps nonfinite floats to null; reject them before serialization.
    json.dumps(value, allow_nan=False)
    try:
        return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode()
    except TypeError:
        # JSON allows arbitrary integer precision; never coerce a large receipt ID into a float.
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def library_status():
    return [
        {"name": name, "version": importlib.metadata.version(name), "role": role}
        for name, role in LIBRARIES.items()
    ]


class Envelope(msgspec.Struct, forbid_unknown_fields=True):
    version: int
    digest: str
    payload: bytes


class TokenMeter:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.encoding = None
        self.attempted = False
        self.memo = cachetools.LRUCache(maxsize=256)

    def warm(self):
        """Explicit initial-setup download; runtime never calls this or accesses the network."""
        import tiktoken_ext.openai_public

        spec = tiktoken_ext.openai_public.cl100k_base()
        ranks = spec["mergeable_ranks"]
        value = {
            "pattern": spec["pat_str"],
            "special": spec["special_tokens"],
            "ranks": [[base64.b64encode(k).decode(), v] for k, v in ranks.items()],
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / "tokenizer.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(canonical(value), encoding="utf-8")
        temporary.replace(path)
        self.encoding = None
        self.attempted = False
        self.memo.clear()
        return {"encoding": "cl100k_base", "path": str(path), "downloaded_during_setup_only": True}

    def count(self, text):
        digest = xxhash.xxh3_128_hexdigest(text.encode())
        if digest in self.memo:
            return self.memo[digest]
        if self.encoding is None and not self.attempted:
            self.attempted = True
            path = self.cache_dir / "tokenizer.json"
            try:
                if path.is_file() and path.stat().st_size <= 8_000_000:
                    spec = orjson.loads(path.read_bytes())
                    self.encoding = tiktoken.Encoding(
                        name="autopilot_cl100k",
                        pat_str=spec["pattern"],
                        mergeable_ranks={base64.b64decode(k, validate=True): v for k, v in spec["ranks"]},
                        special_tokens=spec["special"],
                    )
            except Exception:
                self.encoding = None  # Bad setup assets never trigger a runtime download.
        count = (
            len(self.encoding.encode(text, disallowed_special=())) if self.encoding else len(text.encode())
        )
        result = {"tokens": count, "basis": "cl100k_estimate" if self.encoding else "utf8_upper_bound"}
        self.memo[digest] = result
        return result


class TextReducer:
    def __init__(self):
        self.stemmers = {name: snowballstemmer.stemmer(name) for name in ("english", "russian")}

    def words(self, text):
        words = regex.findall(r"[\p{L}\p{N}]+", text.lower(), timeout=0.1)[:4000]
        language = "russian" if regex.search(r"\p{Cyrillic}", text, timeout=0.1) else "english"
        return self.stemmers[language].stemWords(words)

    def extract_html(self, text, url=""):
        if not regex.search(r"(?i)<(?:html|body|article|main|p|table|div)\b", text, timeout=0.1):
            return text, []
        soup = BeautifulSoup(text, "lxml")
        links = []
        for anchor in soup.find_all("a", href=True)[:100]:
            target = urljoin(url, anchor["href"])
            parsed = urlsplit(target)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            ):
                links.append({"url": target[:2000], "label": anchor.get_text(" ", strip=True)[:120]})
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()
        safe = str(soup)
        if soup.find(["table", "pre", "code"]):
            return markdownify.markdownify(safe, heading_style="ATX"), links[:20]
        article = trafilatura.extract(safe, include_tables=True, include_links=True, favor_recall=True)
        if article:
            return article, links[:20]
        # Explicit parser: extraction never fetches remote entities or executes scripts.
        return html.fromstring(safe, parser=html.HTMLParser(no_network=True)).text_content(), links[:20]

    def reduce(self, text, query, limit=2400, url=""):
        original = text
        text, links = self.extract_html(text[:120000], url)
        text = ftfy.fix_text(text, normalization="NFC")
        lines, fenced = [], False
        for line in text.splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced
            lines.append(
                line
                if fenced or line.startswith(("    ", "\t"))
                else regex.sub(r"[\t ]+", " ", line, timeout=0.1)
            )
        text = "\n".join(lines)
        paragraphs = [p.strip() for p in regex.split(r"\n\s*\n", text, timeout=0.1) if p.strip()]
        segments = []
        for paragraph in paragraphs[:400]:
            if len(paragraph) <= 1200:
                segments.append(paragraph)
            else:
                # Chunk by sentences where possible; never imply reconstructed code is executable.
                sentences = regex.split(r"(?<=[.!?])\s+", paragraph, timeout=0.1)
                for group in chunked(sentences, 4):
                    block = " ".join(group)
                    segments.extend(block[i : i + 1200] for i in range(0, len(block), 1200))
        seen, unique, duplicates, similar = {}, [], 0, 0
        signatures = []
        for segment in segments[:400]:
            digest = xxhash.xxh3_128_hexdigest(segment.encode())
            if digest in seen and seen[digest] == segment:
                duplicates += 1
                continue
            seen[digest] = segment
            tokens = self.words(segment)
            signature = MinHash(num_perm=16, seed=1)
            for token in set(tokens[:300]):
                signature.update(token.encode())
            if len(segment) > 80:
                similar += sum(
                    1
                    for old, sig in signatures[-8:]
                    if signature.jaccard(sig) >= 0.8 and ratio(segment, old) >= 95
                )
            signatures.append((segment, signature))
            unique.append(segment)
        selected = list(range(len(unique)))
        if len("\n\n".join(unique)) > limit and unique:
            corpus = [self.words(p) or ["_"] for p in unique]
            scores = BM25Okapi(corpus).get_scores(self.words(query) or ["_"])
            # Always retain beginning and end; relevance selects the remaining bounded excerpts.
            chosen = {0, len(unique) - 1}
            ranked = sorted(range(len(unique)), key=lambda i: (-float(scores[i]), i))
            used = sum(min(len(unique[i]), limit // 3) for i in chosen)
            for index in ranked:
                if index not in chosen and used + min(len(unique[index]), 800) <= limit:
                    chosen.add(index)
                    used += min(len(unique[index]), 800)
            selected = sorted(chosen)
        excerpt = "\n\n".join(unique[i][: max(80, limit // max(1, len(selected)))] for i in selected)[:limit]
        return excerpt, {
            "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "source_chars": len(original),
            "excerpt_chars": len(excerpt),
            "exact_duplicates": duplicates,
            "near_duplicates_retained": similar,
            "selected_chunks": selected,
            "source_links": links,
            "is_excerpt": excerpt != original,
            "trust": "untrusted source excerpt; not completion proof",
        }


class Economy:
    def __init__(self, directory, text_limit=2400, ttl=120):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache = diskcache.Cache(
            str(self.root / "cache"), size_limit=16 * 1024 * 1024, eviction_policy="least-recently-used"
        )
        self.schemas = cachetools.TTLCache(maxsize=32, ttl=3600)
        self.meter = TokenMeter(self.root)
        self.reducer = TextReducer()
        self.text_limit, self.ttl = text_limit, ttl
        self.failures = cachetools.TTLCache(maxsize=128, ttl=15)

    def close(self):
        self.cache.close()

    def schema(self, schema):
        key = schema.__module__ + "." + schema.__qualname__
        if key not in self.schemas:
            self.schemas[key] = schema.model_json_schema()
        return self.schemas[key]

    def prepare(self, packet, final=False, planning=False):
        original = canonical(packet)
        outgoing = json.loads(original)
        notes = []
        query = str(packet.get("original_brief", ""))[:5000]
        if not final:
            # Only observations are transformed. Instructions, actions, authority and receipts stay exact.
            for action in outgoing.get("recent_actions", []):
                data = (action.get("result") or {}).get("data", {})
                containers = [data]
                if isinstance(data.get("browser"), dict):
                    containers.append(data["browser"])
                for container in containers:
                    value = container.get("text")
                    if isinstance(value, str) and len(value) > self.text_limit:
                        reduced, meta = self.reducer.reduce(
                            value, query, self.text_limit, container.get("url", "")
                        )
                        # Keep original text when the complete annotated excerpt would cost more bytes.
                        if len(canonical({"text": reduced, "excerpt": meta}).encode()) < len(value.encode()):
                            container["text"] = reduced
                            container["excerpt"] = meta
                            notes.append(
                                {
                                    "action_id": action.get("id"),
                                    **{k: v for k, v in meta.items() if k not in {"source_links", "trust"}},
                                }
                            )

        # Titles are schema annotations, not constraints. Stable removal reduces repeated tool metadata.
        def strip_titles(value):
            if isinstance(value, dict):
                return {
                    k: (v if k in {"default", "enum", "const", "examples"} else strip_titles(v))
                    for k, v in value.items()
                    if not (k == "title" and isinstance(v, str))
                }
            if isinstance(value, list):
                return [strip_titles(v) for v in value]
            return value

        if isinstance(outgoing.get("tools"), dict):
            outgoing["tools"] = (
                sorted(outgoing["tools"]) if planning and not final else strip_titles(outgoing["tools"])
            )
        encoded = canonical(outgoing)
        before, after = self.meter.count(original), self.meter.count(encoded)
        report = {
            "before_bytes": len(original.encode()),
            "after_bytes": len(encoded.encode()),
            "before_tokens": before["tokens"],
            "after_tokens": after["tokens"],
            "token_basis": after["basis"],
            "final_evidence_preserved": final,
            "excerpt_count": len(notes),
            "context_digest": blake3.blake3(encoded.encode()).hexdigest(),
        }
        report["manifest_patch"] = jsonpatch.make_patch(
            {"bytes": report["before_bytes"], "tokens": report["before_tokens"]},
            {"bytes": report["after_bytes"], "tokens": report["after_tokens"]},
        ).patch
        return outgoing, report

    def key(self, job_id, model, system, packet, schema):
        return blake3.blake3(
            canonical(["economy-v1", job_id, model, system, packet, self.schema(schema)]).encode()
        ).hexdigest()

    def get_plan(self, key, schema):
        try:
            raw = self.cache.get("plan:" + key)
            if raw is None:
                return None
            if len(raw) > 256000:
                raise ValueError("Oversized cache envelope")
            envelope = msgspec.msgpack.decode(raw, type=Envelope)
            if envelope.version != 1:
                raise ValueError("Unsupported cache format")
            if zstandard.frame_content_size(envelope.payload) > 128000:
                raise ValueError("Oversized decompressed plan")
            payload = zstandard.ZstdDecompressor().decompress(envelope.payload, max_output_size=128000)
            if blake3.blake3(payload).hexdigest() != envelope.digest:
                raise ValueError("Cache integrity failure")
            value = orjson.loads(payload)
            jsonschema.Draft202012Validator(self.schema(schema)).validate(value)
            return schema.model_validate(value)
        except Exception:
            try:
                self.cache.delete("plan:" + key)
            except Exception:
                pass
            return None

    def put_plan(self, key, value):
        payload = canonical(value.model_dump()).encode()
        if len(payload) > 128000:
            return
        envelope = Envelope(
            1, blake3.blake3(payload).hexdigest(), zstandard.ZstdCompressor(level=3).compress(payload)
        )
        try:
            self.cache.set("plan:" + key, msgspec.msgpack.encode(envelope), expire=self.ttl)
        except Exception:
            logging.getLogger(__name__).warning("Optional plan cache unavailable")

    def record(self, job_id, report):
        # Metrics contain counts/hashes only, not prompts, source text, tokens or credentials.
        try:
            with self.cache.transact():
                history = self.cache.get("metrics:" + job_id, [])
                history.append({**report, "at": time.time()})
                self.cache.set("metrics:" + job_id, history[-100:], expire=30 * 86400)
        except Exception:
            logging.getLogger(__name__).warning("Optional economy metrics unavailable")

    def summary(self, job_id):
        rows = self.cache.get("metrics:" + job_id, [])
        totals = {
            k: sum(row.get(k, 0) for row in rows)
            for k in (
                "before_bytes",
                "after_bytes",
                "before_tokens",
                "after_tokens",
                "cache_hit",
                "cloud_attempts",
                "provider_cached_tokens",
                "provider_prompt_tokens",
                "provider_completion_tokens",
            )
        }
        totals.update(
            measurements=len(rows),
            retention="last 100 measurements / 30 days",
            actual_provider_savings_known=False,
        )
        return {"totals": totals, "recent": rows[-10:]}

    def analyze(self, value, query=None):
        if query and len(query) > 500:
            raise ValueError("Projection is limited to 500 characters")
        selected = jmespath.search(query, value) if query else value
        text = selected if isinstance(selected, str) else canonical(selected)
        excerpt, info = self.reducer.reduce(text, "", self.text_limit)
        return {
            "excerpt": excerpt,
            "metadata": info,
            "before": self.meter.count(text),
            "after": self.meter.count(excerpt),
            "wire_bytes": len(text.encode()),
            "compressed_storage_bytes": len(zstandard.ZstdCompressor().compress(text.encode())),
            "compression_is_not_token_savings": True,
        }

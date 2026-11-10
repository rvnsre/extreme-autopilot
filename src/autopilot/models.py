import asyncio
import base64
import json
import logging
import time
import weakref

import httpx
from pydantic import BaseModel

from autopilot.config import Settings
from autopilot.schema import TERMINAL
from autopilot.store import Store


class ModelUnavailable(RuntimeError):
    pass


class ModelDeferred(ModelUnavailable):
    """A local cooldown; no provider request or budget reservation was made."""


class Models:
    def __init__(self, settings: Settings, store: Store):
        self.s, self.store = settings, store
        self.local_context = 0
        self._economy = None
        self._economy_checked = False
        self._plan_locks = weakref.WeakValueDictionary()

    @property
    def economy(self):
        if not self._economy_checked:
            self._economy_checked = True
            if self.s.economy_enabled:
                try:
                    from autopilot.economy import Economy

                    self._economy = Economy(
                        self.s.data_dir / "economy", self.s.economy_text_limit, self.s.economy_plan_ttl
                    )
                except ImportError:
                    logging.getLogger(__name__).warning(
                        "Install the economy extra for local token optimizations"
                    )
        return self._economy

    def close(self):
        if self._economy:
            self._economy.close()

    def _active(self, job_id):
        job = self.store.job(job_id)
        if job["status"] in TERMINAL or time.time() >= job["deadline"]:
            raise ModelUnavailable("Task stopped before model request or cache reuse")

    async def generate(self, job_id: str, system: str, packet: dict, schema: type[BaseModel], cloud=False):
        self._active(job_id)
        economy = self.economy
        if economy is None:
            return await self._generate(job_id, system, packet, schema, cloud)
        from autopilot.economy import canonical

        # Independent verification receives its original evidence, never lossy excerpts or cached verdicts.
        packet, report = economy.prepare(
            packet, final=schema.__name__ == "Verdict", planning=schema.__name__ == "Plan"
        )
        model = ("cloud:" + self.s.cloud_model) if cloud else ("local:" + self.s.local_model)
        report.update(route=model.split(":", 1)[0], schema=schema.__name__, cache_hit=0, cloud_attempts=0)
        key = economy.key(job_id, model, system, packet, schema)
        # Only Plans may be reused; action decisions, screenshots and final verdicts are never cached.
        cacheable = schema.__name__ == "Plan" and not packet.get("latest_screen")
        lock = self._plan_locks.setdefault(key, asyncio.Lock()) if cacheable else asyncio.Lock()
        try:
            async with lock:
                self._active(job_id)
                if cacheable and (cached := economy.get_plan(key, schema)) is not None:
                    report["cache_hit"] = 1
                    return cached
                if cloud and model in economy.failures:
                    raise ModelDeferred("Cloud cooldown active; no new paid request sent")
                report["cloud_attempts"] = int(cloud)
                try:
                    result = await self._generate(job_id, system, packet, schema, cloud)
                except ModelUnavailable:
                    if cloud:
                        economy.failures[model] = True
                    raise
                if cacheable:
                    economy.put_plan(key, result)
                # Validate a stable serialization before retaining any derived result.
                canonical(result.model_dump())
                return result
        finally:
            economy.record(job_id, report)

    async def _request(self, url, body, headers=None):
        # No automatic retries: timeouts can still have been billed by the provider.
        async with httpx.AsyncClient(timeout=120, follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code != 200:
                    raise ModelUnavailable(f"Model HTTP status {response.status_code}")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 1048576:
                        raise ModelUnavailable("Model response exceeds 1 MB")
                return json.loads(data)

    async def _generate(self, job_id: str, system: str, packet: dict, schema: type[BaseModel], cloud=False):
        economy = self.economy
        if economy:
            from autopilot.economy import canonical

            content = canonical(packet)
            schema_json = canonical(economy.schema(schema))
        else:
            content = json.dumps(packet, ensure_ascii=False, allow_nan=False)
            schema_json = json.dumps(schema.model_json_schema())
        output_limit = (
            min(
                self.s.cloud_output_tokens if cloud else 2048,
                {
                    "Plan": self.s.economy_plan_tokens,
                    "Decision": self.s.economy_decision_tokens,
                    "Verdict": self.s.economy_verdict_tokens,
                }.get(schema.__name__, 2048),
            )
            if economy
            else self.s.cloud_output_tokens
        )
        image = packet.get("latest_screen")
        if image:
            if not cloud:
                raise ModelUnavailable("Local text adapter cannot consume images")
            try:
                if not isinstance(image, str) or len(image) > 344000:
                    raise ValueError("Oversized image")
                raw_image = base64.b64decode(image, validate=True)
                if (
                    len(raw_image) > 256000
                    or not raw_image.startswith(b"\xff\xd8")
                    or not raw_image.endswith(b"\xff\xd9")
                ):
                    raise ValueError("Invalid JPEG transport")
            except (ValueError, TypeError) as exc:
                raise ModelUnavailable("Image must be a valid bounded JPEG transport") from exc
        text_content = json.dumps(
            {k: v for k, v in packet.items() if k != "latest_screen"}, ensure_ascii=False
        )
        if len(text_content.encode()) > 180000:
            raise ModelUnavailable("Model context exceeds application limit")
        if not cloud:
            if not self.s.local_model:
                raise ModelUnavailable("EA_LOCAL_MODEL is not configured")
            if not self.local_context:
                try:
                    metadata = await self._request(
                        self.s.ollama_url + "/api/show", {"model": self.s.local_model}
                    )
                    if metadata.get("remote_model") or metadata.get("remote_host"):
                        raise ModelUnavailable("Local adapter rejected a remote Ollama model")
                    limits = [
                        v
                        for k, v in metadata.get("model_info", {}).items()
                        if k.endswith(".context_length") and isinstance(v, int) and v > 0
                    ]
                    if not limits:
                        raise ModelUnavailable("Cannot establish local model context capacity")
                    self.local_context = min(16384, max(limits))
                except Exception as exc:
                    raise ModelUnavailable("Local model metadata unavailable") from exc
            if len((system + content + schema_json).encode()) + 2048 > self.local_context:
                raise ModelUnavailable(
                    "Context exceeds conservative local capacity; escalate instead of truncating"
                )
            current = self.store.job(job_id)
            if current["status"] in TERMINAL or time.time() >= current["deadline"]:
                raise ModelUnavailable("Task stopped before local generation")
            body = {
                "model": self.s.local_model,
                "stream": False,
                "format": schema.model_json_schema(),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "options": {
                    "num_predict": min(2048, output_limit) if economy else 2048,
                    "num_ctx": self.local_context,
                },
                "keep_alive": "10m",
            }
            try:
                data = await self._request(self.s.ollama_url + "/api/chat", body)
                if data.get("done") is not True or data.get("done_reason") != "stop":
                    raise ModelUnavailable("Incomplete local response")
                return schema.model_validate_json(data["message"]["content"])
            except Exception as exc:
                raise ModelUnavailable(f"Local model unavailable: {type(exc).__name__}") from exc

        if not (
            self.s.cloud_model
            and self.s.openai_key.get_secret_value()
            and self.s.input_usd_per_million > 0
            and self.s.output_usd_per_million > 0
        ):
            raise ModelUnavailable("Cloud model, API key and explicit model pricing must be configured")
        # Extract only the most recent screenshot; images never enter text history as base64 strings.
        sanitized = json.loads(content)
        image = sanitized.pop("latest_screen", None)
        clean = canonical(sanitized) if economy else json.dumps(sanitized, ensure_ascii=False)
        user_content = [{"type": "text", "text": clean}]
        if image:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + image, "detail": "low"},
                }
            )
        formatted_system = system + " Return only a JSON object matching: " + schema_json
        reserve = (
            (len((formatted_system + clean).encode()) + 2048 + (self.s.image_token_reserve if image else 0))
            * self.s.input_usd_per_million
            + output_limit * self.s.output_usd_per_million
        ) / 1_000_000
        try:
            call_id = self.store.reserve(job_id, reserve)
        except ValueError as exc:
            raise ModelUnavailable("Cloud budget exhausted") from exc
        try:
            # Local schema validation is authoritative. JSON mode supports provider models that
            # cannot accept Pydantic's discriminated-union schema in strict Structured Outputs.
            body = {
                "model": self.s.cloud_model,
                "store": False,
                "messages": [
                    {"role": "system", "content": formatted_system},
                    {"role": "user", "content": user_content},
                ],
                "max_completion_tokens": output_limit,
                "response_format": {"type": "json_object"},
            }
            data = await self._request(
                "https://api.openai.com/v1/chat/completions",
                body,
                {"Authorization": "Bearer " + self.s.openai_key.get_secret_value()},
            )
            usage = data.get("usage", {})
            if economy:
                # Record reported cached input without assuming a discount or opting into paid cache writes.
                details = usage.get("prompt_tokens_details") or {}
                cached = details.get("cached_tokens", 0)
                if type(cached) is int and cached >= 0:
                    economy.record(
                        job_id,
                        {
                            "provider_cached_tokens": cached,
                            "provider_prompt_tokens": usage.get("prompt_tokens", 0)
                            if type(usage.get("prompt_tokens")) is int
                            else 0,
                            "provider_completion_tokens": usage.get("completion_tokens", 0)
                            if type(usage.get("completion_tokens")) is int
                            else 0,
                        },
                    )
            if type(usage.get("prompt_tokens")) is int and type(usage.get("completion_tokens")) is int:
                if usage["prompt_tokens"] < 0 or usage["completion_tokens"] < 0:
                    raise ModelUnavailable("Invalid model usage")
                actual = (
                    usage["prompt_tokens"] * self.s.input_usd_per_million
                    + usage["completion_tokens"] * self.s.output_usd_per_million
                ) / 1_000_000
                self.store.settle(call_id, actual)
            else:
                self.store.settle(call_id)
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                raise ModelUnavailable("Incomplete or refused cloud response")
            return schema.model_validate_json(choice["message"]["content"])
        except BaseException as exc:
            self.store.settle(call_id)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise ModelUnavailable(f"Cloud request failed: {type(exc).__name__}") from exc

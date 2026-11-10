import asyncio
import json
import time

import httpx
from pydantic import BaseModel

from autopilot.config import Settings
from autopilot.schema import TERMINAL
from autopilot.store import Store


class ModelUnavailable(RuntimeError):
    pass


class Models:
    def __init__(self, settings: Settings, store: Store):
        self.s, self.store = settings, store
        self.local_context = 0

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

    async def generate(self, job_id: str, system: str, packet: dict, schema: type[BaseModel], cloud=False):
        content = json.dumps(packet, ensure_ascii=False, allow_nan=False)
        if len(content.encode()) > 180000:
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
            if (
                len((system + content + json.dumps(schema.model_json_schema())).encode()) + 2048
                > self.local_context
            ):
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
                "options": {"num_predict": 2048, "num_ctx": self.local_context},
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
        clean = json.dumps(sanitized, ensure_ascii=False)
        user_content = [{"type": "text", "text": clean}]
        if image:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + image, "detail": "low"},
                }
            )
        formatted_system = (
            system + " Return only a JSON object matching: " + json.dumps(schema.model_json_schema())
        )
        reserve = (
            (len((formatted_system + clean).encode()) + 2048 + (self.s.image_token_reserve if image else 0))
            * self.s.input_usd_per_million
            + self.s.cloud_output_tokens * self.s.output_usd_per_million
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
                "max_completion_tokens": self.s.cloud_output_tokens,
                "response_format": {"type": "json_object"},
            }
            data = await self._request(
                "https://api.openai.com/v1/chat/completions",
                body,
                {"Authorization": "Bearer " + self.s.openai_key.get_secret_value()},
            )
            usage = data.get("usage", {})
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

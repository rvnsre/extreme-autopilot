"""Deterministic scope checks; semantic authorization still needs model review and an isolated VM."""

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from autopilot.schema import Action

READ_TOOLS = {"observe", "browser_observe", "desktop_observe", "file_read", "file_list", "http_get"}
LOCAL_TOOLS = {"file_write", "process", "desktop_type", "desktop_key", "desktop_click", "desktop_launch"}


def fingerprint(action):
    normalized = {
        "tool": action["tool"],
        "args": {k: v for k, v in action.get("args", {}).items() if k != "frame"},
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scoped_path(root: Path, relative: str):
    if not relative or "\\" in relative or ":" in relative:
        raise ValueError("Use a relative slash-separated workspace path")
    p = Path(relative)
    target = (root / p).resolve()
    if p.is_absolute() or not target.is_relative_to(root.resolve()) or ".git" in p.parts:
        raise ValueError("Path escapes the task workspace or enters Git internals")
    return target


def web_url(url: str, allow_private=False):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only HTTP(S) URLs without credentials are accepted")
    host = parsed.hostname.lower()
    if not allow_private:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("Private network destinations are not enabled")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("Private network destinations are not enabled")
    return url


def validate_authority(prompt: str, action: Action, current_url=""):
    if (
        action.tool == "api_request"
        and action.args.get("method", "GET") != "GET"
        and action.intent != "external_write"
    ):
        raise ValueError("Mutating API requests require external_write intent")
    if action.tool in READ_TOOLS and action.intent != "read":
        raise ValueError("Observation tools must use read intent")
    if action.tool in LOCAL_TOOLS and action.intent == "read":
        raise ValueError("A state-changing tool cannot declare read intent")
    if action.intent in {"local_change", "external_write"}:
        quote = action.authority_quote
        if len(quote.strip()) < 12 or quote not in prompt:
            raise ValueError("A state change needs a verbatim authorization quote from the original brief")
    if action.intent == "external_write":
        destination = action.args.get("url") or current_url
        host = urlsplit(destination).hostname
        urls = re.findall(r"https?://[^\s<>\"']+", prompt)
        if not host or host not in {urlsplit(u.rstrip(".,;)")).hostname for u in urls}:
            raise ValueError("External writes require the exact destination URL in the original brief")

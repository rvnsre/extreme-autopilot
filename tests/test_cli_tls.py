import asyncio
import json
import ssl
import sys

import pytest
from pydantic import ValidationError

from autopilot.cli import doctor, main, tls_certificate
from autopilot.config import Settings


def test_tls_certificate_matches_configured_host(tmp_path):
    info = tls_certificate(tmp_path / "tls", ["localhost", "127.0.0.1"])
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(info["certificate"], info["private_key"])
    client = ssl.create_default_context(cafile=info["certificate"])
    assert client.verify_mode == ssl.CERT_REQUIRED
    assert client.check_hostname
    with pytest.raises(ValueError):
        tls_certificate(tmp_path / "tls", ["localhost"])


def test_doctor_does_not_emit_credentials(settings):
    result = asyncio.run(doctor(settings))
    assert result["cloud_key_configured"]
    assert "test-key" not in json.dumps(result)
    assert "a" * 48 not in json.dumps(result)


def test_token_cli_generates_unique_tokens(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["autopilot", "token"])
    main()
    first = capsys.readouterr().out.strip()
    main()
    second = capsys.readouterr().out.strip()
    assert first != second and len(first) >= 32


@pytest.mark.parametrize("url", ["http://remote.example", "https://127.0.0.1", "file:///etc/passwd"])
def test_ollama_must_be_local(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_url=url)


def test_worker_tls_cannot_be_disabled():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, server_url="http://127.0.0.1:8766")


def test_example_tokens_cannot_start_a_server(settings):
    from pydantic import SecretStr

    settings.api_token = SecretStr("REPLACE_WITH_FIRST_GENERATED_TOKEN_AT_LEAST_32_CHARACTERS")
    with pytest.raises(ValueError):
        settings.require_tokens()

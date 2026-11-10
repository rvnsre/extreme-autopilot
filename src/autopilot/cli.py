import argparse
import asyncio
import datetime
import ipaddress
import json
import secrets
import ssl
import sys
import threading
from pathlib import Path

import httpx

from autopilot.config import Settings, token_configured


def tls_certificate(directory, hosts):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = directory / "worker.key", directory / "worker.pem"
    if key_path.exists() or cert_path.exists():
        raise ValueError("Certificate already exists; rotation must be explicit")
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Extreme Autopilot worker gateway")])
    alternatives = []
    for host in hosts:
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            alternatives.append(x509.DNSName(host))
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    with key_path.open("xb") as out:
        out.write(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return {"certificate": str(cert_path), "private_key": str(key_path), "expires_days": 90}


async def doctor(s, models=False):
    checks = {
        "api_token_configured": token_configured(s.api_token),
        "worker_token_configured": token_configured(s.worker_token),
        "local_model_configured": bool(s.local_model),
        "cloud_model_configured": bool(s.cloud_model),
        "cloud_key_configured": bool(s.openai_key.get_secret_value()),
        "pricing_configured": s.input_usd_per_million > 0 and s.output_usd_per_million > 0,
    }
    if models:
        async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
            try:
                response = await client.post(s.ollama_url + "/api/show", json={"model": s.local_model})
                response.raise_for_status()
                data = response.json()
                checks["ollama_local_model_available"] = not (
                    data.get("remote_model") or data.get("remote_host")
                )
                checks["ollama_capabilities"] = data.get("capabilities", [])
            except Exception as exc:
                checks["ollama_error"] = type(exc).__name__
            try:
                response = await client.get(
                    "https://api.openai.com/v1/models/" + s.cloud_model,
                    headers={"Authorization": "Bearer " + s.openai_key.get_secret_value()},
                )
                checks["cloud_model_accessible"] = response.status_code == 200
            except Exception as exc:
                checks["cloud_error"] = type(exc).__name__
    return checks


def main():
    parser = argparse.ArgumentParser(prog="autopilot")
    parser.add_argument("--env-file", default=".env")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("token", help="Print a new random token; never commit it")
    cert = sub.add_parser("tls", help="Generate a gateway certificate; copy only the public PEM into the VM")
    cert.add_argument("--host", action="append", required=True)
    sub.add_parser("worker")
    sub.add_parser("browser-setup", help="One-time task-account login setup inside the dedicated VM")
    diag = sub.add_parser("doctor")
    diag.add_argument("--models", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--worker-bind", default="127.0.0.1")
    serve.add_argument("--cert", type=Path, required=True)
    serve.add_argument("--key", type=Path, required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--file", type=Path, required=True)
    submit.add_argument("--budget", type=float, default=5)
    submit.add_argument("--seconds", type=int, default=86400)
    submit.add_argument(
        "--request-key", help="Reuse this key to retry the same submission without duplication"
    )
    show = sub.add_parser("status")
    show.add_argument("id")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("id")
    report = sub.add_parser("report")
    report.add_argument("id")
    sub.add_parser("jobs")
    args = parser.parse_args()
    if args.command == "token":
        print(secrets.token_urlsafe(48))
        return
    s = Settings(_env_file=args.env_file)
    if args.command == "tls":
        print(json.dumps(tls_certificate(s.data_dir / "tls", args.host), indent=2))
    elif args.command == "doctor":
        print(json.dumps(asyncio.run(doctor(s, args.models)), ensure_ascii=False, indent=2))
    elif args.command == "worker":
        from autopilot.worker import run_worker

        asyncio.run(run_worker(s))
    elif args.command == "browser-setup":
        if not s.isolated_worker:
            parser.error("Browser account setup is only allowed in the dedicated worker VM")
        from playwright.sync_api import sync_playwright

        profile = s.browser_profile_template or s.data_dir / "profiles" / "template"
        profile.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                str(profile), headless=False, channel=s.browser_channel
            )
            # This prompt is initial setup only; no task execution path reads console input.
            input(
                "Complete task-only account logins in this browser, then press Enter here to finish setup: "
            )
            browser.close()
        print("Set EA_BROWSER_PROFILE_TEMPLATE=" + str(profile))
    elif args.command == "serve":
        import uvicorn

        from autopilot.api import create_app

        if not args.cert.is_file() or not args.key.is_file():
            parser.error("Gateway TLS certificate and private key are required")
        # Validate key/certificate before starting either server.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        gateway = uvicorn.Server(
            uvicorn.Config(
                create_app(s, worker_only=True),
                host=args.worker_bind,
                port=8766,
                ssl_certfile=str(args.cert),
                ssl_keyfile=str(args.key),
                access_log=False,
            )
        )
        thread = threading.Thread(target=gateway.run, daemon=True)
        thread.start()
        try:
            uvicorn.run(create_app(s), host="127.0.0.1", port=8765, access_log=False)
        finally:
            gateway.should_exit = True
            thread.join(timeout=10)
    else:
        url = "http://127.0.0.1:8765/api/jobs"
        with httpx.Client(
            timeout=30, trust_env=False, headers={"Authorization": "Bearer " + s.api_token.get_secret_value()}
        ) as client:
            if args.command == "submit":
                request_key = args.request_key or secrets.token_urlsafe(24)
                try:
                    response = client.post(
                        url,
                        headers={"Idempotency-Key": request_key},
                        json={
                            "prompt": args.file.read_text(encoding="utf-8-sig"),
                            "budget_usd": args.budget,
                            "max_seconds": args.seconds,
                        },
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    print(
                        json.dumps(
                            {
                                "error": type(exc).__name__,
                                "request_key": request_key,
                                "retry": "Retry with --request-key and the same brief/limits",
                            }
                        ),
                        file=sys.stderr,
                    )
                    raise SystemExit(1) from None
            elif args.command == "cancel":
                response = client.post(url + "/" + args.id + "/cancel")
            else:
                suffix = "" if args.command == "jobs" else "/" + args.id
                if args.command == "report":
                    suffix += "/report"
                response = client.get(url + suffix)
            response.raise_for_status()
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

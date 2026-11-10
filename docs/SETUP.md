# Windows coordinator + dedicated VM

## Upgrading from v0.1.0

Stop the coordinator and worker before upgrading. Back up each complete private data directory while stopped, including databases, journals and any WAL files. Keep backups private. Install v0.2.0 on **both** machines, rebuild the panel, then start the coordinator and worker using the existing configuration. Database additions are automatic, transactional and serialized. An active legacy coordinator prevents migration.

Worker protocol 2 adds a per-process `X-Worker-Session` header and a 45-second single-owner lease, managed automatically by the supplied worker. Mixed v0.1/v0.2 services are unsupported. A new worker may need to wait for the previous lease to expire. Existing incomplete mutations are not replayed; unknown outcomes produce a partial report and prohibit further changes in that task. Do not delete a worker journal to try to resume an ambiguous operation.

Rolling back requires stopping both services and restoring the matching pre-upgrade data backups; old code is not compatible with the added worker columns/protocol. No downgrade or data deletion is performed automatically.

## Topology and prerequisites

The coordinator and Ollama run on the host. A standard, non-administrator Windows user runs the worker inside a separate Windows 11 VM. Choose a hypervisor supported by your Windows edition. Install and license Windows yourself; this project does not install a hypervisor, modify security settings, accept license terms or automate account creation.

VM starting allocation: 4 vCPUs, 8 GB RAM, 60 GB free disk, 1280×800 desktop. This is a starting configuration, not a measured capacity guarantee. Host resources must also fit the chosen Ollama model. Disable host-folder/clipboard/drive sharing in the hypervisor during initial setup and create a clean VM snapshot. Use NAT for public web access plus a host-only adapter for the coordinator gateway. Do not expose port 8766 to the public Internet.

Install Python 3.11+, Node 22.12+ (supported 22/24/26 release lines), Git, Chromium and any task-specific programs inside the VM from official sources. Pre-establish any required application login using task-only accounts. No OpenAI API key is needed in the VM.

## Coordinator

1. Run `scripts/Install-Coordinator.ps1` in the source checkout.
2. Copy `.env.example` to a private directory outside every Git checkout, for example `C:\AutopilotData\coordinator.env`. Restrict that directory to your Windows account using normal Windows access controls.
3. Generate **two different** tokens with `.\.venv\Scripts\autopilot.exe token`; assign them to `EA_API_TOKEN` and `EA_WORKER_TOKEN`. Do not put tokens in URLs or shell history commands containing literal values.
4. Configure `EA_LOCAL_MODEL` with the exact installed Ollama model. Run Ollama with cloud disabled (`OLLAMA_NO_CLOUD=1` in its server environment). Use an actually local model; remote/cloud aliases are unsupported. Ensure its structured JSON output works before acceptance.
5. Set `EA_OPENAI_KEY`, `EA_CLOUD_MODEL`, `EA_INPUT_USD_PER_MILLION` and `EA_OUTPUT_USD_PER_MILLION`. Use a model supporting image input, JSON mode and Chat Completions. Read current [model pricing](https://developers.openai.com/api/docs/pricing); this project deliberately does not guess rates for arbitrary models. The OpenAI account/budget is independent of a ChatGPT subscription.
6. Run `autopilot --env-file C:\AutopilotData\coordinator.env doctor --models`. A successful model listing only proves access, not agent quality; run the live acceptance cases too.
7. Generate a 90-day TLS certificate for the host-only IP:

```powershell
.\.venv\Scripts\autopilot.exe --env-file C:\AutopilotData\coordinator.env tls --host 192.168.56.1 --host localhost
```

Copy only `worker.pem` to the VM. Keep `worker.key` on the host. No certificate-warning bypass or `verify=False` is used. Rotate before expiry and distribute the new public certificate during maintenance.

Start the coordinator (replace IP and paths with your host-only adapter address and generated file paths):

```powershell
.\.venv\Scripts\autopilot.exe --env-file C:\AutopilotData\coordinator.env serve --worker-bind 192.168.56.1 --cert C:\AutopilotData\coordinator\tls\worker.pem --key C:\AutopilotData\coordinator\tls\worker.key
```

The panel binds only to `127.0.0.1:8765`. The authenticated TLS worker listener binds to the explicitly selected host-only address. Configure any necessary firewall access manually for that private VM network only.

## Worker, inside the VM only

```powershell
$env:EA_ISOLATED_WORKER='true'
.\scripts\Install-Worker.ps1
```

Create a private `C:\AutopilotData\worker.env` with these values; keep the **worker token** identical to the coordinator setting:

```dotenv
EA_DATA_DIR=C:/AutopilotData/worker
EA_WORKER_TOKEN=YOUR_WORKER_TOKEN
EA_SERVER_URL=https://192.168.56.1:8766
EA_CA_FILE=C:/AutopilotData/worker/worker.pem
EA_ISOLATED_WORKER=true
EA_HEADLESS=false
```

```powershell
.\.venv\Scripts\autopilot.exe --env-file C:\AutopilotData\worker.env worker
```

Use an interactive, unlocked Windows console session. Do not install the GUI worker as a Session 0 Windows service. For startup, use a user logon task in Task Scheduler that runs only while that user is logged on. A locked/disconnected desktop may prevent GUI input; the agent reports the obstacle rather than disabling security protections. Power loss and a locked login screen cannot be solved by application code.

Each task receives a distinct workspace and persistent browser profile. For authenticated browser tasks, run the following **once during setup**, inside the VM:

```powershell
.\.venv\Scripts\autopilot.exe --env-file C:\AutopilotData\worker.env browser-setup
```

Log in to task-only accounts manually, finish setup, and set `EA_BROWSER_PROFILE_TEMPLATE` in the worker env file to the printed directory. Close the setup browser before starting the worker. The template is copied into each new task profile; later task changes do not modify the template. This copies authentication cookies, so protect the template as a credential. Never point it at a personal browser profile. Expired sessions and MFA/CAPTCHA obstacles result in a blocked/partial report without a runtime question.

For API-first authenticated operations, configure named credentials in the **worker** env file, for example `EA_API_CREDENTIALS={"github":{"origin":"https://api.github.com","token":"YOUR_TASK_ACCOUNT_TOKEN"}}`. Configure only the public origin map on the **coordinator**: `EA_API_PROFILE_ORIGINS={"github":"https://api.github.com"}`. Use fine-grained task-account credentials. The model sees profile names and origins, not raw tokens. An external-write brief must explicitly name the API destination URL. The current Codex GitHub plugin connection is not exported into the installed application.

## Verify before unattended use

Open the local panel and enter the coordinator API token. Confirm worker connectivity. Run the three `examples/*.txt` tasks with local and cloud models configured. Review actual output files, test exit codes and desktop observations, not only model summaries. Then run restart/failure cases and the 24-hour soak described in VALIDATION.md.

For frontend development only, set `EA_UI_ORIGINS=["http://127.0.0.1:5173","http://localhost:5173","http://127.0.0.1:8765"]` and run `npm run dev` in `web/`. Do not expand origins to arbitrary remote sites.

# Windows, Linux and macOS

v0.3.0 supports a coordinator and a dedicated worker on Windows, Linux or macOS. Host and worker OSes can differ; the HTTPS worker protocol is unchanged (version 2). Upgrade both services while stopped and preserve their private data/journals. Never share a personal desktop or host folders with an unattended worker.

| Capability | Windows | Linux | macOS |
|---|---|---|---|
| FastAPI, panel, CLI, SQLite, Ollama/OpenAI routing | Yes | Yes | Yes |
| Playwright browser, API, file tools | Yes | Yes | Yes |
| Bounded subprocess lifecycle | Windows Job Object | POSIX process group | POSIX process group |
| Native desktop | UI Automation + native input | X11 window metadata + native input | Accessibility/System Events + CoreGraphics input |
| Desktop prerequisites | Unlocked interactive session | Unlocked X11, EWMH window manager, `wmctrl`, `xdotool`, Pillow XCB | Accessibility, Screen Recording and Automation permissions, unlocked login session |

The POSIX process group manages ordinary children. A hostile process can deliberately detach; this is not equivalent to Windows Job Object containment or an OS sandbox. All platforms still require a disposable VM or dedicated machine and task-only accounts. Virtualize macOS only on hardware and under terms supported by Apple.

## Installation

Python 3.11+ and Node 22.12+ are required. From a fresh source checkout:

**Windows:** use `scripts/Install-Coordinator.ps1` on the host and `scripts/Install-Worker.ps1` in the dedicated worker, following [SETUP.md](SETUP.md).

**Linux/macOS coordinator:**

```bash
bash scripts/install.sh coordinator
.venv/bin/autopilot token
# Create a private env file outside the checkout; follow SETUP.md for TLS and model configuration.
.venv/bin/autopilot --env-file /absolute/private/coordinator.env doctor --models
```

**Linux/macOS worker:** run inside the dedicated VM/machine:

```bash
EA_ISOLATED_WORKER=true bash scripts/install.sh worker
.venv/bin/autopilot --env-file /absolute/private/worker.env doctor --desktop
.venv/bin/autopilot --env-file /absolute/private/worker.env worker
```

The installer does not run `sudo`, change system permissions, install a display server or provision accounts. On Linux install Chromium's OS libraries (`.venv/bin/python -m playwright install-deps chromium`, with appropriate administrator privileges) during initial setup. Install `wmctrl`, `xdotool` and an EWMH window manager through your distribution for desktop tools. Debian/Ubuntu example: `sudo apt-get install wmctrl xdotool openbox`. Use an actual X11 login session; do not start a second window manager over an existing one. The CI-only Xvfb/Openbox script is a synthetic test harness.

Native Wayland desktop automation is unsupported, including sessions with XWayland enabled. Browser automation can still run with Playwright (headless mode does not need desktop input). A headless server can use browser/API/file/process tools; desktop input fails with an actionable diagnostic.

Chromium sandboxing stays enabled. Some Ubuntu/AppArmor installations restrict unprivileged user namespaces; provision an appropriate executable-specific AppArmor profile during worker setup, or use an administrator-provisioned browser with a matching profile via `EA_BROWSER_CHANNEL`. The disposable Linux CI runner installs a profile for its Playwright browser path rather than disabling sandboxing or globally disabling AppArmor restrictions. The application never changes host policy.

## macOS first-time permissions

In System Settings → Privacy & Security, grant the dedicated worker's terminal/Python launcher **Accessibility**, **Screen & System Audio Recording** (name varies by OS version), and **Automation → System Events**. Use the same launcher for setup and execution; restarting it may be necessary. During initial setup only, a local operator can run:

```bash
/usr/bin/osascript -e 'tell application "System Events" to get name of every application process'
```

This can request the initial Automation consent. Complete it before tasks begin. `doctor --desktop` and task execution preflight existing permissions without requesting them; missing permission is a blocked tool result. They never enable permissions or run `tccutil` automatically. Refer to [Apple's UI scripting guide](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AutomatetheUserInterface.html).

macOS handles are process IDs representing **each application's front window**, not stable IDs for all windows in an app. Switching documents changes the title/bounds guard and requires observation again. Only the front window of each app is exposed. Screen captures may have Retina pixel scaling; mouse coordinates are window-local OS logical coordinates from `bounds`, never coordinates measured directly in the resized JPEG. Use `command` for macOS shortcuts, e.g. `["command","s"]`; use `ctrl` on Linux/Windows. Native macOS hotkeys support letters, digits, Enter/Tab/Space/Backspace/Escape/Delete, arrows, Home/End/PageUp/PageDown and ctrl/alt/shift/command modifiers. Unsupported chords fail rather than guessing key codes.

## Configuration and diagnostics

Use absolute paths for `EA_DATA_DIR`, `EA_CA_FILE` and browser templates. Do not copy Windows `C:/...` example paths into Unix configuration. Defaults:

- Windows: `%LOCALAPPDATA%/ExtremeAutopilot`.
- Linux: `$XDG_DATA_HOME/ExtremeAutopilot`, or `~/.local/share/ExtremeAutopilot` (relative XDG paths are ignored).
- macOS: `~/Library/Application Support/ExtremeAutopilot`.

`EA_DESKTOP_APPS` maps aliases to **executable files**, never shell commands. Windows defaults to `notepad`; macOS to the TextEdit executable. Linux has no default editor because distributions differ; for example set `EA_DESKTOP_APPS={"editor":"/usr/bin/gedit"}` if that application is installed. All platforms may override the map. Launching an allowed executable starts a detached GUI process in the task workspace; it is not a bounded `process` command. Close these task-only GUI apps when retiring a worker VM.

`observe` exposes platform/backend capabilities so models can choose appropriate tools. `doctor --desktop` additionally tests the local display/permissions and reports `desktop_ready` plus a bounded error. Run it on the worker, not on the coordinator. A negative desktop probe does not disable browser/API/file/process tools. Linux exposes window metadata and screenshots, not a fabricated accessibility tree.

## Verification scope

CI covers Windows/Linux/macOS × Python 3.11/3.12, real Chromium on all three, and real X11 Unicode typing/screenshot/readback in a disposable Xvfb/Openbox session. macOS CI runs native permission preflight without granting TCC access; it does **not** certify unattended native GUI input. Validate native desktop permission, typing, shortcuts, capture and file readback on your actual dedicated macOS worker before unattended use. Live model, authenticated site and full 24-hour acceptance still follow [VALIDATION.md](../VALIDATION.md).

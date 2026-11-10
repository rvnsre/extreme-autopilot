"""Regenerate version pins from a reviewed Windows verification environment.

Pins are portable across the CI Python 3.11/3.12 matrix. Windows-only desktop dependencies use markers.
Regenerate on Windows, which supplies the largest dependency closure; verify all three OSes in CI.
"""

import importlib.metadata as metadata
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def closure(roots):
    pending, result = list(roots), {}
    while pending:
        requirement = Requirement(pending.pop())
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        if name in result:
            continue
        dist = metadata.distribution(name)
        result[name] = dist.version
        for spec in dist.requires or []:
            child = Requirement(spec)
            if child.marker and not any(
                child.marker.evaluate({"extra": extra}) for extra in ["", *requirement.extras]
            ):
                continue
            child.marker = None  # Already evaluated in the parent's extras context.
            pending.append(str(child))
    return result


def main():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = project["dependencies"] + ["setuptools>=75", "wheel"]
    groups = {
        "coordinator": core,
        "economy": core + project["optional-dependencies"]["economy"],
        "worker": core + project["optional-dependencies"]["worker"],
        "dev": core + project["optional-dependencies"]["dev"] + ["playwright", "pillow", "pywin32"],
    }
    for group, roots in groups.items():
        pins = closure(roots)
        lines = ["# Version-pinned verification environment; regenerate with scripts/lock_dependencies.py."]
        if group == "worker":
            lines.append("# Windows/Linux/macOS worker; Windows-only UI dependencies are marked.")
        for name, version in sorted(pins.items()):
            windows_only = {
                "pywin32",
                "comtypes",
                "pywinauto",
                "pyautogui",
                "mouseinfo",
                "pygetwindow",
                "pymsgbox",
                "pyperclip",
                "pyrect",
                "pyscreeze",
                "pytweening",
                "six",
            }
            marker = (
                "; sys_platform == 'win32'"
                if name in {"pywin32", "comtypes"} or (group == "worker" and name in windows_only)
                else ""
            )
            lines.append(f"{name}=={version}{marker}")
        (ROOT / f"requirements-{group}.lock.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

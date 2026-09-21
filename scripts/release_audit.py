#!/usr/bin/env python3
"""Audit the repository candidate without claiming legal clearance."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PUBLIC_FILES = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "OPEN_SOURCE_RELEASE.md",
)
RUNTIME_PREFIXES = ("data/", "reports/", ".venv/", ".venv-")
LOCAL_PATH_MARKERS = ("D:" + "\\Agent\\supply-chain-agent", "D:" + "/Agent/supply-chain-agent")
TEXT_SUFFIXES = {".md", ".html", ".json", ".py", ".js", ".css", ".yml", ".yaml", ".txt", ".sh", ".bat", ".command"}


def candidate_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return sorted(line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip())


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    for name in REQUIRED_PUBLIC_FILES:
        if not (ROOT / name).is_file():
            failures.append(f"missing required public file: {name}")

    if not (ROOT / "LICENSE").is_file():
        failures.append("missing root LICENSE; the repository is not ready for open-source distribution")
    if not (ROOT / "NOTICE").is_file():
        failures.append("missing root NOTICE required by this project's Apache-2.0 release policy")

    upstream = ROOT / "UPSTREAM.md"
    if not upstream.is_file() or "PUBLIC_RELEASE_AUTHORIZED: true" not in upstream.read_text(encoding="utf-8"):
        failures.append("UPSTREAM.md does not record project-owner public release authorization")

    try:
        files = candidate_files()
    except (OSError, subprocess.CalledProcessError) as exc:
        failures.append(f"cannot enumerate Git release candidates: {exc}")
        files = []

    for relative in files:
        if relative == "TEMPLATE_SOURCE_MANIFEST.json":
            failures.append("legacy TEMPLATE_SOURCE_MANIFEST.json is included in the release candidate")
        if relative.startswith(RUNTIME_PREFIXES):
            failures.append(f"runtime artifact is included in the release candidate: {relative}")

        path = ROOT / relative
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in LOCAL_PATH_MARKERS:
            if marker in content:
                failures.append(f"local development path remains in {relative}: {marker}")

    warnings.append("retain external evidence supporting the owner declaration in UPSTREAM.md")
    warnings.append("run a dedicated secret scanner against the complete Git history before publishing")

    for warning in warnings:
        print(f"WARN  {warning}")
    if failures:
        for failure in sorted(set(failures)):
            print(f"FAIL  {failure}")
        print(f"release audit: FAIL ({len(set(failures))} blocking issue(s))")
        return 1

    print(f"PASS  {len(files)} candidate file(s) checked")
    print("release audit: PASS (manual rights confirmation still applies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

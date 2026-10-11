"""The air-gap guard: the runtime package must contain no HTTP client (spec §9).

All download code lives in ``install.sh``; ``src/utter`` must never reach the network. This
grep is the structural half of the guarantee -- the dynamic half (zero non-unix socket
syscalls under ``strace``) is Phase 3's.

The scan is anchored to this file's repository, not the working directory, and it asserts it
read a plausible number of modules: a mis-rooted scan must fail rather than pass with zero
offenders and zero files read. The ``socket.socket(`` alternative excludes any call that names
``AF_UNIX`` in the same parentheses -- the control channel legitimately opens unix sockets
(spec §9 explicitly allows AF_UNIX uses), so a naive match would flag the permitted case.
"""

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "utter"

MIN_MODULES = 12
"""A floor under the scanned-file count. The package holds ~15 modules, so a wrong root,
which finds 0 or a handful of files, fails this before the offender list can be believed."""

_NETWORK_CLIENT = re.compile(
    r"\b(?:requests|httpx|urllib3|aiohttp|http\.client)\b"
    r"|\burllib\.request\b"
    r"|\bfrom urllib import request\b"
    r"|\bsocket\.create_connection\b"
    r"|\bsocket\.socket\((?![^)]*AF_UNIX)"
)


def test_no_http_client_in_the_runtime_package():
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert len(files) >= MIN_MODULES, (
        f"scan rooted at {PACKAGE_ROOT} found only {len(files)} modules; wrong root?"
    )
    offenders = [
        f"{path.relative_to(PACKAGE_ROOT.parent.parent)}:{number}"
        for path in files
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if _NETWORK_CLIENT.search(line)
    ]
    assert offenders == [], f"runtime package must not touch the network: {offenders}"

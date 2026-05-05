"""WSL discovery — enumerate installed distros and propose them as SSH hosts.

We use SSH (not the \\\\wsl$\\ bridge) so the sync code path stays single. WSL2
forwards distro localhost ports to Windows localhost, so once `sshd` is
running inside the distro, you can `ssh user@localhost` from Windows.

This module:
1) Enumerates WSL distros via `wsl --list --verbose` (UTF-16 LE output).
2) For each, runs `whoami`, `$HOME`, and probes whether sshd is listening.
3) Returns suggestions that the UI can one-click into the existing
   remote_hosts SSH flow.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field


WSL_LIST_ENCODING = "utf-16-le"


@dataclass
class WslDistro:
    name: str
    state: str
    is_default: bool
    user: str | None = None
    home_dir: str | None = None
    ssh_running: bool = False
    ssh_port: int = 22
    sshd_installed: bool = False
    suggested_projects_path: str | None = None
    error: str | None = None
    hint: str | None = None  # actionable next step if ssh_running is false


def _decode_wsl_output(b: bytes) -> str:
    if b.startswith(b"\xff\xfe"):
        b = b[2:]
    return b.decode(WSL_LIST_ENCODING, errors="replace")


async def _run_wsl(args: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    """Run `wsl <args>`; returns (rc, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "wsl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        return 127, "", f"{type(e).__name__}: {e}"
    rc = proc.returncode or 0
    # `wsl --list` emits UTF-16 LE; `wsl -d X -- cmd` emits whatever the inner cmd does (typically utf-8).
    if args and args[0] == "--list":
        out = _decode_wsl_output(stdout)
    else:
        out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    return rc, out, err


_LIST_HEADER = re.compile(r"^\s*NAME\b", re.IGNORECASE)


async def discover_distros() -> list[WslDistro]:
    rc, out, _ = await _run_wsl(["--list", "--verbose"], timeout=8)
    if rc != 0 or not out.strip():
        return []

    distros: list[WslDistro] = []
    for raw in out.splitlines():
        if not raw.strip():
            continue
        if _LIST_HEADER.match(raw):
            continue
        is_default = raw.lstrip().startswith("*")
        line = raw.lstrip().lstrip("*").strip()
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        state = parts[1] if len(parts) > 1 else "Unknown"
        if name.startswith("docker-desktop"):
            continue  # helper distros
        distros.append(WslDistro(name=name, state=state, is_default=is_default))

    # Probe each distro in parallel — saves time when there are many.
    await asyncio.gather(*(_probe(d) for d in distros), return_exceptions=False)
    return distros


async def _probe(d: WslDistro) -> None:
    """Fill in user / home / ssh status for one distro.

    We make several small `wsl -d ... -- sh -c <single-cmd>` calls instead of
    one big composite script. Windows argv quoting / wsl.exe's pass-through
    layer mangles `$(...)` command substitutions and backslash-heavy regexes,
    so each subprocess call sticks to one straightforward command, and parsing
    happens here in Python.
    """
    async def sh(cmd: str, timeout: float = 8.0) -> tuple[int, str]:
        rc, out, err = await _run_wsl(
            ["-d", d.name, "--", "sh", "-c", cmd], timeout=timeout
        )
        return rc, (out + err)

    try:
        # 1) whoami + home in one cheap call
        rc, out = await sh("whoami; echo HOMELINE=$HOME")
        if rc != 0:
            d.error = out.strip()[:200] or f"rc={rc}"
            return
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not lines:
            d.error = "no whoami output"
            return
        d.user = lines[0]
        for ln in lines:
            if ln.startswith("HOMELINE="):
                d.home_dir = ln.removeprefix("HOMELINE=").strip() or None
                break
        if not d.home_dir and d.user:
            d.home_dir = f"/home/{d.user}"
        d.suggested_projects_path = (
            f"{d.home_dir.rstrip('/')}/.claude/projects" if d.home_dir else None
        )

        # 2) sshd installed?
        rc, out = await sh("command -v sshd && echo SSHD_OK || echo SSHD_NO")
        d.sshd_installed = "SSHD_OK" in out

        # 3) Configured port — read the config file(s) directly; parse in Python.
        rc, out = await sh(
            "cat /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null"
        )
        d.ssh_port = _parse_sshd_port(out) or 22

        # 4) Listening on that port? Use ss; parse here.
        rc, out = await sh("ss -ltnH 2>/dev/null")
        d.ssh_running = _ss_listening_on(out, d.ssh_port)

        if not d.ssh_running:
            if d.sshd_installed:
                d.hint = (
                    f"sshd is installed (configured for port {d.ssh_port}) but not "
                    f"listening. Start it with:\n"
                    f"  wsl -d {d.name} -- sudo service ssh start\n"
                    f"or:\n"
                    f"  wsl -d {d.name} -- sudo systemctl start ssh"
                )
            else:
                d.hint = (
                    "Install + start sshd inside the distro:\n"
                    f"  wsl -d {d.name} -- sudo apt install -y openssh-server\n"
                    f"  wsl -d {d.name} -- sudo service ssh start"
                )
    except Exception as e:
        d.error = f"{type(e).__name__}: {e}"


def _parse_sshd_port(text: str) -> int | None:
    """Find the first uncommented `Port <num>` directive."""
    import re

    for line in text.splitlines():
        m = re.match(r"^\s*Port\s+(\d+)\b", line)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def _ss_listening_on(text: str, port: int) -> bool:
    """Check whether `ss -ltnH` output has any LISTEN line on the given port."""
    needle = f":{port}"
    for line in text.splitlines():
        # Each LISTEN line has columns: state recv send local-addr peer-addr
        # We only care that the local-addr ends with :<port>, not :<port-prefix>.
        for tok in line.split():
            if tok.endswith(needle) and not tok.endswith(f"{port * 10}"):
                # Confirm port is the trailing numeric part after the last ':'
                tail = tok.rsplit(":", 1)[-1]
                if tail.isdigit() and int(tail) == port:
                    return True
    return False

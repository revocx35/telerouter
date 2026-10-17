"""SSH plumbing: telerouter's own client key, host-key pinning (trust on first use, confirmed by the admin)."""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path

import asyncssh

from ..store import write_private

log = logging.getLogger("telerouter.ssh")

CONNECT_TIMEOUT = 15


class SshKeys:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "ssh" / "id_ed25519"
        if not self.path.exists():
            key = asyncssh.generate_private_key("ssh-ed25519", comment="telerouter")
            write_private(self.path, key.export_private_key())
            log.info("generated the SSH key telerouter uses to reach agent hosts")
        self.key = asyncssh.read_private_key(str(self.path))

    @property
    def public(self) -> str:
        return self.key.export_public_key().decode().strip()


async def fetch_host_key(host: str, port: int) -> str:
    """The server's host key in OpenSSH format (shown to the admin before it is trusted)."""
    key = await asyncio.wait_for(asyncssh.get_server_host_key(
        host, port, server_host_key_algs=["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512", "rsa-sha2-256"]),
        CONNECT_TIMEOUT)
    if key is None:
        raise OSError("no host key received")
    return key.export_public_key().decode().strip()


def fingerprint(openssh_key: str) -> str:
    try:
        return asyncssh.import_public_key(openssh_key).get_fingerprint("sha256")
    except (asyncssh.KeyImportError, ValueError):
        return "?"


async def connect(host: str, port: int, username: str, keys: SshKeys, host_key: str) -> asyncssh.SSHClientConnection:
    if not host_key:
        raise PermissionError("the host key is not confirmed yet")
    trusted = asyncssh.import_public_key(host_key)
    return await asyncssh.connect(
        host, port, username=username, client_keys=[keys.key], known_hosts=([trusted], [], []),
        agent_path=None, password=None, kbdint_auth=False, connect_timeout=CONNECT_TIMEOUT,
        keepalive_interval=20, keepalive_count_max=3)


def q(s: str) -> str:
    return shlex.quote(s)


def remote_path(home: str, path: str) -> str:
    """Expand a leading ~ against the remote $HOME (we never let the remote shell expand anything)."""
    if path == "~":
        return home
    if path.startswith("~/"):
        return home.rstrip("/") + path[1:]
    return path

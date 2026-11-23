"""Run a read-only shell command inside the task container an API request came from.

The proxy sits outside the task environment, so for Whole-file Carry-over (optlimit/carry.py) it has
to read files of the container itself.  Docker only: the container is found from the source address
of the request.  Harbor >= 0.20 puts the task's `main` service in the network namespace of an
egress-control sidecar, so the address belongs to the sidecar; the `main` container of the same
compose project is used then.  Anything that fails returns (1, "") and the caller carries nothing.

OPTLIMIT_DOCKER names the client binary (default "docker").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

logger = logging.getLogger("optlimit")
MAIN_SERVICE = "main"


async def _docker(*args: str, timeout: float = 20.0) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(os.environ.get("OPTLIMIT_DOCKER", "docker"), *args,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, b""
    return proc.returncode or 0, out


def pick_container(inspected: list[dict], ip: str) -> str | None:
    """Id of the task container for a request from `ip`, given `docker inspect` output of the running containers."""
    hit = None
    for c in inspected:
        nets = ((c.get("NetworkSettings") or {}).get("Networks") or {})
        if any((n or {}).get("IPAddress") == ip for n in nets.values()):
            hit = c
            break
    if hit is None:
        return None
    labels = ((hit.get("Config") or {}).get("Labels") or {})
    project = labels.get("com.docker.compose.project")
    if project and labels.get("com.docker.compose.service") != MAIN_SERVICE:
        for c in inspected:
            lb = ((c.get("Config") or {}).get("Labels") or {})
            if lb.get("com.docker.compose.project") == project and lb.get("com.docker.compose.service") == MAIN_SERVICE:
                return c.get("Id")
    return hit.get("Id")


class DockerExec:
    """`await run(ip, cmd)` -> (return code, stdout text) in the container that owns `ip`."""

    def __init__(self) -> None:
        self._by_ip: dict[str, str] = {}

    async def container(self, ip: str) -> str | None:
        cid = self._by_ip.get(ip)
        if cid:
            return cid
        rc, out = await _docker("ps", "-q")
        ids = out.decode().split()
        if rc != 0 or not ids:
            return None
        rc, out = await _docker("inspect", *ids)
        try:
            inspected = json.loads(out)
        except ValueError:
            return None
        cid = pick_container(inspected if isinstance(inspected, list) else [], ip)
        if cid:
            self._by_ip[ip] = cid
        return cid

    async def run(self, ip: str, cmd: str, timeout: float = 20.0) -> tuple[int, str]:
        try:
            cid = await self.container(ip)
            if not cid:
                return 1, ""
            rc, out = await _docker("exec", cid, "sh", "-c", cmd, timeout=timeout)
            if rc != 0 and not out:
                # a plain miss, or the container is gone and a later trial reuses the address: look it up again
                self._by_ip.pop(ip, None)
                new = await self.container(ip)
                if new and new != cid:
                    rc, out = await _docker("exec", new, "sh", "-c", cmd, timeout=timeout)
            return rc, out.decode("utf-8", "replace")
        except Exception:
            logger.exception("envexec: docker exec failed")
            return 1, ""

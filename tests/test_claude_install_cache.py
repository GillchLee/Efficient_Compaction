"""Host-cached Claude Code binary + robust in-container install (optlimit.adapters.harbor_installed)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from optlimit.adapters import harbor_installed as hi


def _seed(cache: Path, ver: str, data: bytes = b"bin") -> Path:
    d = cache / ver; d.mkdir(parents=True)
    (d / "claude").write_bytes(data)
    return d / "claude"


def test_cache_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("OPTLIMIT_CLAUDE_CACHE", "0")
    assert hi.claude_cache_dir() is None
    assert hi.cached_claude_binary() is None


def test_unpinned_uses_newest_cached_without_network(tmp_path, monkeypatch):
    _seed(tmp_path, "2.1.9"); newest = _seed(tmp_path, "2.1.322")
    monkeypatch.setattr(hi, "_curl", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    assert hi.cached_claude_binary(None, tmp_path) == newest
    assert hi.cached_claude_binary("latest", tmp_path) == newest


def test_pinned_version_downloads_and_verifies(tmp_path, monkeypatch):
    payload = b"claude-binary"; sha = hashlib.sha256(payload).hexdigest()
    calls = []
    def fake_curl(url, out=None, timeout=20):
        calls.append(url)
        if url.endswith("manifest.json"):
            return json.dumps({"platforms": {"linux-x64": {"checksum": sha, "size": len(payload)}}})
        assert out is not None; Path(out).write_bytes(payload); return ""
    monkeypatch.setattr(hi, "_curl", fake_curl)
    p = hi.cached_claude_binary("2.1.400", tmp_path)
    assert p == tmp_path / "2.1.400" / "claude" and p.read_bytes() == payload and p.stat().st_mode & 0o111
    assert hi.cached_claude_binary("2.1.400", tmp_path) == p and len(calls) == 2  # cached now


def test_checksum_mismatch_returns_none(tmp_path, monkeypatch):
    def fake_curl(url, out=None, timeout=20):
        if url.endswith("manifest.json"):
            return json.dumps({"platforms": {"linux-x64": {"checksum": "0" * 64}}})
        Path(out).write_bytes(b"x"); return ""
    monkeypatch.setattr(hi, "_curl", fake_curl)
    assert hi.cached_claude_binary("2.1.401", tmp_path) is None
    assert not (tmp_path / "2.1.401" / "claude").exists()


class _Env:
    def __init__(self): self.uploads = []; self.cmds = []
    async def upload_file(self, src, dst): self.uploads.append((Path(src), dst))
    async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None, **kw):
        self.cmds.append((user, command))
        class R: return_code = 0; stdout = ""; stderr = ""
        return R()


@pytest.mark.asyncio
async def test_install_uses_cache_and_native_layout(tmp_path, monkeypatch):
    cache = tmp_path / "cache"; binary = _seed(cache, "2.1.322")
    monkeypatch.setenv("OPTLIMIT_CLAUDE_CACHE", str(cache))
    a = hi.OptlimitClaudeCode(logs_dir=tmp_path / "logs", model_name="anthropic/claude-haiku-5-5")
    env = _Env()
    async def no(*_a, **_k): return False
    async def deps(*_a, **_k): env.cmds.append(("root", "apt-get install ..."))
    monkeypatch.setattr(a, "_installed_claude_satisfies_version", no)
    monkeypatch.setattr(hi.ClaudeCode, "ensure_system_dependencies", deps)
    monkeypatch.setattr(a, "exec_as_root", lambda e, command, **k: env.exec(command, user="root", **k))
    monkeypatch.setattr(a, "exec_as_agent", lambda e, command, **k: env.exec(command, user="agent", **k))
    await a.install(env)
    assert env.uploads == [(binary, "/tmp/optlimit-claude")]
    joined = "\n".join(c for _, c in env.cmds)
    assert "getent hosts" in joined                      # DNS wait before apt
    assert "apt-get install" in joined
    assert "versions/$V" in joined and "V='2.1.322'" in joined or "V=2.1.322" in joined
    assert "ln -sfn" in joined and "claude --version" in joined
    assert "bootstrap.sh" not in joined                  # no in-container download


@pytest.mark.asyncio
async def test_install_falls_back_to_stock_without_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTLIMIT_CLAUDE_CACHE", "0")
    a = hi.OptlimitClaudeCode(logs_dir=tmp_path / "logs", model_name="anthropic/claude-haiku-5-5")
    env = _Env()
    async def no(*_a, **_k): return False
    async def deps(*_a, **_k): pass
    monkeypatch.setattr(a, "_installed_claude_satisfies_version", no)
    monkeypatch.setattr(hi.ClaudeCode, "ensure_system_dependencies", deps)
    monkeypatch.setattr(a, "exec_as_root", lambda e, command, **k: env.exec(command, user="root", **k))
    monkeypatch.setattr(a, "exec_as_agent", lambda e, command, **k: env.exec(command, user="agent", **k))
    await a.install(env)
    assert env.uploads == [] and any("bootstrap.sh" in c for _, c in env.cmds)


@pytest.mark.asyncio
async def test_package_step_retries_after_failure(tmp_path, monkeypatch):
    a = hi.OptlimitClaudeCode(logs_dir=tmp_path / "logs", model_name="anthropic/claude-haiku-5-5")
    env = _Env(); n = {"k": 0}
    async def flaky(*_a, **_k):
        n["k"] += 1
        if n["k"] < 3: raise RuntimeError("Could not resolve archive.ubuntu.com")
    monkeypatch.setattr(hi.ClaudeCode, "ensure_system_dependencies", flaky)
    monkeypatch.setattr(a, "exec_as_root", lambda e, command, **k: env.exec(command, user="root", **k))
    async def fast(_s): pass
    monkeypatch.setattr(hi.asyncio, "sleep", fast)
    await a.ensure_system_dependencies(env, ("curl",))
    assert n["k"] == 3 and sum("getent hosts" in c for _, c in env.cmds) == 3

"""remote.py — control plane for the REMOTE prop-firm simulation.

The forward sim runs on a free GitHub Actions runner (private repo
`Phyeory/prop-sim`).  The dashboard is its control panel: it reads the
account state the runner commits back, and it starts/stops the runner.

All state lives in the repo under `runtime/`:
    state.json       the raw resume state (book, cursor, desired, pending)
    account.json     the dashboard-ready account snapshot (equity, cash,
                     positions, guard phase, floors, counters)
    equity.json      the equity curve: [[epoch, equity], ...]
    trades.json      {"open": [...], "closed": [...]} — round trips
    paused           presence = the sim is stopped (no new decisions)
    armed.json       {"mode": paper|real|off, "config": …} — the arm switch;
                     REAL means live orders on the FTMO account via the
                     Windows VPS MT5 bridge (SSH tunnel from the runner)

Credentials: the GitHub token comes from the local git credential helper
(macOS Keychain) — never from a file in the repo.

Every function is failure-tolerant: the dashboard must never 500 because
GitHub is slow, rate-limited or unauthenticated.
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

REPO = "Phyeory/prop-sim"
WORKFLOW = "prop-sim.yml"
BRANCH = "main"
API = "https://api.github.com"
RUNTIME = "runtime"
ARMED = f"{RUNTIME}/armed.json"
PAPER_CONFIG = "paper_ftmo_sim_gh.json"   # resume-based paper sim (paper broker)
REAL_CONFIG = "gh_prop.json"              # FTMO MT5 via SSH tunnel to the VPS bridge
LOCAL_POLLER_LABEL = "com.jaime.market-prop-sim"  # Mac poller kept OFF while remote armed


# ── credentials ────────────────────────────────────────────────────────────
def _token() -> Optional[str]:
    """PAT from the git credential helper (Keychain)."""
    try:
        out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if line.startswith("password=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
        # Some credential helpers (osxkeychain) put the token in username
        for line in out.splitlines():
            if line.startswith("username=") and line.split("=", 1)[1].strip().startswith("ghp_"):
                return line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def _have_token() -> bool:
    return bool(_token())


# ── GitHub API ─────────────────────────────────────────────────────────────
def api(method: str, path: str, body: Optional[dict] = None,
        timeout: int = 25) -> tuple[Optional[dict], Optional[str]]:
    tok = _token()
    if not tok:
        return None, "no GitHub credential (git credential fill)"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return (json.loads(raw) if raw else {}), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def read_text(path: str) -> Optional[str]:
    """File content from the repo (None if missing)."""
    data, err = api("GET", f"/repos/{REPO}/contents/{path}?ref={BRANCH}")
    if err or not isinstance(data, dict):
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def read_json(path: str) -> Optional[dict]:
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def write_text(path: str, content: str, message: str) -> tuple[bool, str]:
    """Create/update a file on the branch (sha-aware)."""
    existing, err = api("GET", f"/repos/{REPO}/contents/{path}?ref={BRANCH}")
    sha = (existing or {}).get("sha") if isinstance(existing, dict) else None
    body = {"message": message, "content": base64.b64encode(
        content.encode()).decode(), "branch": BRANCH}
    if sha:
        body["sha"] = sha
    data, err = api("PUT", f"/repos/{REPO}/contents/{path}", body)
    if err:
        return False, err
    return True, (data or {}).get("commit", {}).get("html_url", "ok")


def _rm(path: str) -> tuple[bool, str]:
    existing, err = api("GET", f"/repos/{REPO}/contents/{path}?ref={BRANCH}")
    if err or not isinstance(existing, dict) or not existing.get("sha"):
        return True, "already absent"
    data, err = api("DELETE", f"/repos/{REPO}/contents/{path}",
                    {"message": f"remove {path}", "sha": existing["sha"],
                     "branch": BRANCH})
    if err:
        return False, err
    return True, "removed"


# ── arm switch ─────────────────────────────────────────────────────────────
def read_armed() -> dict:
    """The arm switch.  Absent/legacy/unknown → paper (the default mode)."""
    data = read_json(ARMED)
    if not isinstance(data, dict) or data.get("mode") not in ("off", "paper", "real"):
        return {"mode": "paper", "config": PAPER_CONFIG, "ts": 0}
    data.setdefault("config", REAL_CONFIG if data["mode"] == "real" else PAPER_CONFIG)
    return data


def _owned_secrets() -> list:
    """Names of repo Actions secrets ([] if unverifiable)."""
    data, _err = api("GET", f"/repos/{REPO}/actions/secrets")
    if not isinstance(data, dict):
        return []
    return [s.get("name", "") for s in data.get("secrets", [])]


# ── control actions ────────────────────────────────────────────────────────
def start(mode: str = "paper", config: Optional[str] = None,
          confirm: bool = False) -> dict:
    """Arm the runner in `mode` (paper|real|off), un-pause, dispatch a run now.

    REAL mode moves actual money: it changes runtime/armed.json to
    {"mode": "real", "config": "gh_prop.json"} so every scheduled and
    dispatched run trades the FTMO MT5 account through the VPS bridge.
    Requires confirm=True and the three PROP_VPS_* repo secrets.
    """
    out: dict = {"ok": False, "actions": []}
    mode = (mode or "paper").strip().lower()
    if mode not in ("paper", "real", "off"):
        out["error"] = f"unknown mode {mode!r} (paper|real|off)"
        return out
    if mode == "real" and not confirm:
        out["error"] = ("REAL-money arming requires confirm=true "
                        "(FTMO account bought + Windows VPS bridge running + "
                        "PROP_VPS_* repo secrets set)")
        return out
    if mode == "real":
        owned = _owned_secrets()
        if owned:
            missing = [s for s in ("PROP_VPS_HOST", "PROP_VPS_USER", "PROP_VPS_SSH_KEY")
                       if s not in owned]
            if missing:
                out["error"] = ("REAL arm blocked — repo secrets missing: "
                                + ", ".join(missing))
                return out
        # secrets unverifiable → still arm; every real run fails LOUDLY at the
        # tunnel step if the bridge is down, which is a safe no-op.
    cfg = config or (REAL_CONFIG if mode == "real" else PAPER_CONFIG)
    ok, msg = write_text(ARMED, json.dumps(
        {"mode": mode, "config": cfg, "armed_at": time.time()}) + "\n",
        f"prop-sim: arm {mode}")
    out["actions"].append({"arm": ok, "detail": msg})
    if not ok:
        out["error"] = f"committing armed.json failed: {msg}"
        return out
    if mode == "off":
        out["ok"] = True           # disarm only; stop() is the stronger hammer
        return out
    ok, msg = _rm(f"{RUNTIME}/paused")
    out["actions"].append({"unpause": ok, "detail": msg})
    data, err = api("POST", f"/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
                    {"ref": BRANCH})
    if err:
        out["error"] = f"dispatch failed: {err}"
        return out
    out["actions"].append({"dispatch": True})
    out["ok"] = True
    out["mode"] = mode
    out["config"] = cfg
    out["started_at"] = time.time()
    return out


def stop() -> dict:
    """Disarm (mode=off), pause the sim, cancel any in-flight run.

    Also unloads the local Mac prop poller so two schedulers never
    double-submit against the same account.
    """
    out: dict = {"ok": False, "actions": []}
    ok, msg = write_text(ARMED, json.dumps(
        {"mode": "off", "config": None, "disarmed_at": time.time()}) + "\n",
        "prop-sim: disarm (mode=off)")
    out["actions"].append({"disarm": ok, "detail": msg})
    ok, msg = write_text(f"{RUNTIME}/paused",
                         json.dumps({"paused_at": time.time()}) + "\n",
                         "prop-sim: pause")
    out["actions"].append({"pause": ok, "detail": msg})
    runs, err = api("GET", f"/repos/{REPO}/actions/runs?status=in_progress&per_page=10")
    if not err and isinstance(runs, dict):
        cancelled = 0
        for r in runs.get("workflow_runs", []):
            _, e = api("POST", f"/repos/{REPO}/actions/runs/{r['id']}/cancel")
            if not e:
                cancelled += 1
        out["actions"].append({"cancelled_runs": cancelled})
    try:
        uid = subprocess.run(["id", "-u"], capture_output=True, text=True,
                             timeout=5).stdout.strip() or "501"
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LOCAL_POLLER_LABEL}"],
                       capture_output=True, timeout=10)
        out["actions"].append({"local_poller": "launchd bootout attempted"})
    except Exception:  # noqa: BLE001 — local hygiene must never break the call
        pass
    out["ok"] = True
    return out


def list_runs(limit: int = 8) -> list[dict]:
    data, err = api("GET", f"/repos/{REPO}/actions/runs?per_page={limit}")
    if err or not isinstance(data, dict):
        return []
    return [{
        "id": r.get("id"),
        "status": r.get("status"),
        "conclusion": r.get("conclusion"),
        "event": r.get("event"),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        "run_number": r.get("run_number"),
    } for r in data.get("workflow_runs", [])]


def status() -> dict:
    """Everything the dashboard needs about the remote sim."""
    out: dict = {
        "repo": REPO,
        "workflow": WORKFLOW,
        "authenticated": _have_token(),
        "schedule": "05:00 · 05:30 · 08:00 UTC (daily)",
    }
    if not out["authenticated"]:
        out["error"] = ("no GitHub credential — run `git credential approve` "
                         "or sign in to github.com")
        return out

    paused = read_text(f"{RUNTIME}/paused") is not None
    out["paused"] = paused
    armed = read_armed()
    out["armed"] = armed
    out["mode"] = armed.get("mode", "paper")
    account = read_json(f"{RUNTIME}/account.json") or {}
    out["account"] = account
    state = read_json(f"{RUNTIME}/state.json") or {}
    out["state"] = {"cursor": sorted(set((state.get("last_ts") or {}).values())),
                    "book": state.get("book"),
                    "pending": state.get("pending") or {},
                    "saved_at": state.get("saved_at")}
    eq = read_json(f"{RUNTIME}/equity.json") or []
    out["equity"] = eq if isinstance(eq, list) else []
    trades = read_json(f"{RUNTIME}/trades.json") or {}
    out["trades_open"] = trades.get("open") or []
    out["trades_closed"] = trades.get("closed") or []
    runs = list_runs(8)
    out["runs"] = runs
    out["last_run"] = runs[0] if runs else None
    out["last_commit"] = (read_json(f"{RUNTIME}/account.json") or {}).get("updated_at")
    return out

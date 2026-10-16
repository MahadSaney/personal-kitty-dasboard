#!/usr/bin/env python3
"""Per-mission kanban board for Spec Kitty missions.

Read-only: it never writes to a repo or a mission. One local server per
mission, so several missions can each have their own board at the same time.

Commands (all print JSON on stdout):
    start   --repo PATH [--mission SLUG|MID8|ID] [--no-open]
    stop    --repo PATH --mission ...  |  --all
    list
    resolve --repo PATH
    serve   (internal; started by `start`)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BOARD_HTML = HERE / "board.html"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "state")) / "kitty-dashboard"
BOARDS_DIR = STATE_DIR / "boards"
LOGS_DIR = STATE_DIR / "logs"

PORT_BASE = 8700
PORT_SPAN = 250
CACHE_TTL_S = 2.0
PLANNING_ACTIVE_DAYS = 14

LANES = ["planned", "claimed", "in_progress", "for_review", "in_review", "approved", "done", "blocked", "canceled"]
TERMINAL = {"done", "canceled"}
LANE_WEIGHT = {
    "planned": 0.0,
    "claimed": 0.1,
    "in_progress": 0.35,
    "for_review": 0.6,
    "in_review": 0.7,
    "approved": 0.9,
    "done": 1.0,
    "blocked": 0.35,
}
LANE_LABEL = {
    "planned": "Planned",
    "claimed": "Claimed",
    "in_progress": "In progress",
    "for_review": "For review",
    "in_review": "In review",
    "approved": "Approved",
    "done": "Done",
    "blocked": "Blocked",
    "canceled": "Canceled",
}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class CliError(Exception):
    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra = extra


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def parse_ts(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        return EPOCH
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return EPOCH
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime | None) -> str | None:
    if dt is None or dt == EPOCH:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=NO_WINDOW,
    )


def humanize(name: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", name).replace("_", " ").split()
    if not words:
        return name
    text = " ".join(words).lower()
    return text[:1].upper() + text[1:]


def actor_name(actor: Any) -> str:
    if isinstance(actor, dict):
        for key in ("display", "profile", "id", "role", "tool"):
            if actor.get(key):
                return str(actor[key])
        return ""
    return str(actor or "")


def scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value in ("null", "~"):
        return None
    if value in ("true", "false"):
        return value == "true"
    return value


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Top-level scalars and lists from YAML frontmatter; nested maps are ignored."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, Any] = {}
    key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t-":
            item = line.strip()
            if key and item.startswith("- ") and isinstance(data.get(key), list):
                data[key].append(scalar(item[2:]))
            continue
        if ":" not in line:
            continue
        name, _, raw = line.partition(":")
        key = name.strip()
        raw = raw.strip()
        if raw == "":
            data[key] = []
        elif raw.startswith("[") and raw.endswith("]"):
            data[key] = [scalar(x) for x in raw[1:-1].split(",") if x.strip()]
        else:
            data[key] = scalar(raw)
    return data


# ---------------------------------------------------------------------------
# Repo and mission discovery
# ---------------------------------------------------------------------------


def find_repo(start: str) -> Path:
    path = Path(start).expanduser().resolve()
    if ".worktrees" in path.parts:
        path = Path(*path.parts[: path.parts.index(".worktrees")])
    candidates = (path, *path.parents)
    for cand in candidates:
        if (cand / ".kittify").is_dir() and (cand / "kitty-specs").is_dir():
            return cand
    for cand in candidates:
        if (cand / ".kittify").is_dir():
            raise CliError(f"{cand} is Kittified but has no kitty-specs/ folder, so there are no missions yet.")
    raise CliError(f"No Kittified repo (.kittify/) found at or above {path}.")


def list_missions(repo: Path) -> list[Path]:
    specs = repo / "kitty-specs"
    if not specs.is_dir():
        return []
    return sorted(d for d in specs.iterdir() if d.is_dir() and (d / "meta.json").is_file())


def match_mission(repo: Path, query: str) -> Path:
    missions = list_missions(repo)
    if not missions:
        raise CliError(f"No missions found in {repo / 'kitty-specs'}.")
    q = query.strip().lower()

    def ids(d: Path) -> set[str]:
        meta = read_json(d / "meta.json") or {}
        return {d.name.lower(), str(meta.get("mission_id", "")).lower(), str(meta.get("mid8", "")).lower()} - {""}

    exact = [d for d in missions if q in ids(d)]
    if len(exact) == 1:
        return exact[0]
    partial = [d for d in missions if q in d.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise CliError(f"No mission matching '{query}' in {repo}.", candidates=[d.name for d in missions][-20:])
    raise CliError(f"'{query}' matches several missions; be more specific.", candidates=[d.name for d in partial])


# ---------------------------------------------------------------------------
# Where a mission's status lives (checkout vs coordination branch)
# ---------------------------------------------------------------------------


class MissionSource:
    def __init__(self, repo: Path, slug: str, meta: dict, use_git: bool = True) -> None:
        self.repo = repo
        self.slug = slug
        self.local_dir = repo / "kitty-specs" / slug
        self.dir = self.local_dir
        self.branch: str | None = None
        self.mode = "checkout"
        self.detail = "Reading the mission folder in the repo checkout."

        branch = meta.get("coordination_branch")
        if not branch or meta.get("flattened") is True:
            return
        coord_dir = repo / ".worktrees" / f"{slug}-coord" / "kitty-specs" / slug
        if coord_dir.is_dir():
            self.dir = coord_dir
            self.mode = "coordination-worktree"
            self.detail = f"Reading the coordination worktree for {branch}."
            return
        if use_git:
            for ref in (branch, f"origin/{branch}"):
                if git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0:
                    self.branch = ref
                    self.mode = "coordination-branch"
                    self.detail = f"Reading {ref} with git (status lives on the coordination branch)."
                    return
        self.mode = "checkout-may-lag"
        self.detail = (
            f"The coordination branch {branch} isn't available locally, "
            "so this shows the checkout copy, which may lag behind."
        )

    def _git_path(self, rel: str) -> str:
        return f"kitty-specs/{self.slug}/{rel}"

    def read(self, rel: str) -> str:
        if self.branch:
            res = git(self.repo, "show", f"{self.branch}:{self._git_path(rel)}")
            if res.returncode == 0:
                return res.stdout.lstrip("\ufeff")
        try:
            return (self.dir / rel).read_text(encoding="utf-8-sig")
        except OSError:
            if self.dir != self.local_dir:
                try:
                    return (self.local_dir / rel).read_text(encoding="utf-8-sig")
                except OSError:
                    return ""
            return ""

    def task_files(self) -> list[tuple[str, str]]:
        names: list[str] = []
        if self.branch:
            res = git(self.repo, "ls-tree", "--name-only", self.branch, self._git_path("tasks/"))
            if res.returncode == 0:
                names = [Path(line).name for line in res.stdout.splitlines() if line.strip()]
        if not names:
            for base in (self.dir, self.local_dir):
                tasks = base / "tasks"
                if tasks.is_dir():
                    names = [p.name for p in tasks.iterdir()]
                    break
        names = sorted(n for n in names if re.match(r"WP\d+.*\.md$", n))
        return [(n, self.read(f"tasks/{n}")) for n in names]

    def fingerprint(self) -> str:
        if self.branch:
            res = git(self.repo, "rev-parse", self.branch)
            return f"git:{res.stdout.strip()}"
        parts = []
        for rel in ("status.events.jsonl", "meta.json"):
            try:
                st = (self.dir / rel).stat()
                parts.append(f"{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append("-")
        tasks = self.dir / "tasks"
        try:
            parts.append(str(max((p.stat().st_mtime_ns for p in tasks.iterdir()), default=0)))
        except OSError:
            parts.append("-")
        return "fs:" + "|".join(parts)


# ---------------------------------------------------------------------------
# Building the board
# ---------------------------------------------------------------------------


def parse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def event_name(e: dict) -> str:
    return str(e.get("event_type") or e.get("type") or e.get("event_name") or "")


def event_time(e: dict) -> datetime:
    payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    return parse_ts(e.get("at") or e.get("timestamp") or payload.get("at") or payload.get("created_at"))


def is_lane(e: dict) -> bool:
    return bool(e.get("wp_id")) and bool(e.get("to_lane"))


def is_annotation(e: dict) -> bool:
    return e.get("kind") == "annotation" and bool(e.get("wp_id"))


def clip(text: Any, limit: int = 180) -> str | None:
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def real_profile(value) -> bool:
    # Spec Kitty writes sentinels such as "__resolved_profile_absent__" when no profile resolved.
    return bool(value) and not str(value).startswith("__")


def _note_body(item: dict) -> str:
    text = item.get("note") if item["kind"] == "lane" else item["text"].split(": ", 1)[-1]
    return (text or "").strip()[:60]


def echoes_recent(item: dict, activity: list[dict]) -> bool:
    """Drop a note that repeats the reason of a nearby lane move for the same WP (and vice versa)."""
    if item["kind"] not in ("note", "lane"):
        return False
    body = _note_body(item)
    if not body:
        return False
    for i in range(len(activity) - 1, max(-1, len(activity) - 5), -1):
        prev = activity[i]
        if prev.get("wp") != item.get("wp") or prev["kind"] not in ("note", "lane") or prev["kind"] == item["kind"]:
            continue
        if _note_body(prev) == body:
            if item["kind"] == "note":
                return True
            del activity[i]
            return False
    return False


def describe(e: dict) -> dict | None:
    at = iso(event_time(e))
    payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    if is_lane(e):
        frm = e.get("from_lane")
        text = f"{e['wp_id']}: {LANE_LABEL.get(frm, frm or 'new')} → {LANE_LABEL.get(e['to_lane'], e['to_lane'])}"
        return {
            "at": at,
            "kind": "lane",
            "wp": e["wp_id"],
            "lane": e["to_lane"],
            "text": text,
            "actor": actor_name(e.get("actor")),
            "note": clip(e.get("reason")),
        }
    if is_annotation(e):
        delta = e.get("delta") if isinstance(e.get("delta"), dict) else {}
        if delta.get("note"):
            return {"at": at, "kind": "note", "wp": e["wp_id"], "text": f"{e['wp_id']}: {clip(delta['note'], 160)}",
                    "actor": actor_name(e.get("actor"))}
        subs = delta.get("subtasks")
        if isinstance(subs, dict) and subs:
            done = sum(1 for v in subs.values() if v == "done")
            return {"at": at, "kind": "subtasks", "wp": e["wp_id"],
                    "text": f"{e['wp_id']}: ticked off {done} of {len(subs)} subtasks", "actor": actor_name(e.get("actor"))}
        if real_profile(delta.get("agent_profile")):
            return {"at": at, "kind": "assign", "wp": e["wp_id"],
                    "text": f"{e['wp_id']}: picked up by {delta['agent_profile']}", "actor": actor_name(e.get("actor"))}
        return None
    name = event_name(e)
    if not name:
        return None
    if name == "DecisionPointOpened":
        return {"at": at, "kind": "question", "text": f"Asked: {clip(payload.get('question'), 160)}"}
    if name.startswith("DecisionPoint"):
        answer = payload.get("final_answer") or payload.get("terminal_outcome") or ""
        return {"at": at, "kind": "answer", "text": f"Answered: {clip(answer, 160)}" if answer else humanize(name)}
    if name == "WPCreated":
        title = payload.get("wp_title")
        wp = payload.get("wp_id") or e.get("aggregate_id")
        return {"at": at, "kind": "created", "wp": wp, "text": f"{wp} created: {clip(title, 120)}" if title else f"{wp} created"}
    return {"at": at, "kind": "phase", "text": humanize(name), "actor": actor_name(payload.get("actor") or e.get("actor"))}


def build_board(repo: Path, mission_dir: Path, use_git: bool = True) -> dict:
    meta = read_json(mission_dir / "meta.json") or {}
    slug = str(meta.get("mission_slug") or mission_dir.name)
    src = MissionSource(repo, slug, meta, use_git=use_git)
    live_meta = read_json(src.dir / "meta.json") if src.dir != src.local_dir else None
    if isinstance(live_meta, dict):
        meta = {**meta, **live_meta}

    events = parse_events(src.read("status.events.jsonl"))
    events.sort(key=lambda e: (event_time(e), str(e.get("event_id", ""))))

    wps: dict[str, dict] = {}

    def ensure(wp_id: str) -> dict:
        if wp_id not in wps:
            wps[wp_id] = {
                "id": wp_id,
                "title": None,
                "lane": "planned",
                "phase": None,
                "agent_profile": None,
                "agent": None,
                "dependencies": [],
                "dependents": [],
                "subtasks_total": 0,
                "subtasks_done": 0,
                "file": None,
                "last_transition_at": None,
                "actor": None,
                "review": None,
                "note": None,
                "history": [],
            }
        return wps[wp_id]

    for name, text in src.task_files():
        fm = parse_frontmatter(text)
        wp_id = str(fm.get("work_package_id") or name.split("-")[0])
        w = ensure(wp_id)
        w["file"] = name
        w["title"] = fm.get("title") or w["title"]
        w["phase"] = fm.get("phase") or None
        w["agent_profile"] = fm.get("agent_profile") if real_profile(fm.get("agent_profile")) else None
        w["agent"] = fm.get("agent") or None
        deps = fm.get("dependencies")
        if isinstance(deps, list):
            w["dependencies"] = [str(d) for d in deps if d]
        subs = fm.get("subtasks")
        if isinstance(subs, list):
            w["subtasks_total"] = len(subs)

    decisions: dict[str, dict] = {}
    phase_seen: set[str] = set()
    activity: list[dict] = []
    last_event_at = EPOCH

    for e in events:
        at = event_time(e)
        last_event_at = max(last_event_at, at)
        name = event_name(e)
        payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}

        if is_lane(e):
            w = ensure(str(e["wp_id"]))
            w["lane"] = str(e["to_lane"])
            w["last_transition_at"] = iso(at)
            w["actor"] = actor_name(e.get("actor")) or w["actor"]
            evidence = e.get("evidence") if isinstance(e.get("evidence"), dict) else {}
            review = evidence.get("review") if isinstance(evidence.get("review"), dict) else None
            if review and review.get("verdict"):
                w["review"] = {"verdict": review.get("verdict"), "reviewer": review.get("reviewer")}
            w["history"].append({
                "from": e.get("from_lane"),
                "to": e["to_lane"],
                "at": iso(at),
                "actor": actor_name(e.get("actor")),
                "reason": clip(e.get("reason"), 240),
            })
        elif is_annotation(e):
            w = ensure(str(e["wp_id"]))
            delta = e.get("delta") if isinstance(e.get("delta"), dict) else {}
            subs = delta.get("subtasks")
            if isinstance(subs, dict) and subs:
                w["subtasks_total"] = max(w["subtasks_total"], len(subs))
                w["subtasks_done"] = sum(1 for v in subs.values() if v == "done")
            if delta.get("note"):
                w["note"] = {"text": clip(delta["note"], 240), "at": iso(at)}
            if real_profile(delta.get("agent_profile")):
                w["agent_profile"] = w["agent_profile"] or str(delta["agent_profile"])
        elif name == "WPCreated":
            wp_id = payload.get("wp_id") or e.get("aggregate_id")
            if wp_id:
                w = ensure(str(wp_id))
                w["title"] = w["title"] or payload.get("wp_title")
                if not w["dependencies"] and isinstance(payload.get("depends_on"), list):
                    w["dependencies"] = [str(d) for d in payload["depends_on"]]
        elif name == "DecisionPointOpened":
            dp = payload.get("decision_point_id")
            if dp:
                decisions[dp] = {
                    "id": dp,
                    "question": payload.get("question"),
                    "options": [o for o in (payload.get("options") or []) if isinstance(o, str)],
                    "opened_at": iso(at),
                }
        elif name.startswith("DecisionPoint"):
            decisions.pop(payload.get("decision_point_id"), None)
        if name:
            phase_seen.add(name)

        item = describe(e)
        if item:
            if not echoes_recent(item, activity):
                activity.append(item)

    for w in wps.values():
        for dep in w["dependencies"]:
            if dep in wps:
                wps[dep]["dependents"].append(w["id"])

    def wp_key(wp_id: str) -> tuple[int, str]:
        m = re.match(r"WP(\d+)", wp_id)
        return (int(m.group(1)) if m else 10**6, wp_id)

    work_packages = [wps[k] for k in sorted(wps, key=wp_key)]
    counts = {lane: 0 for lane in LANES}
    for w in work_packages:
        counts[w["lane"]] = counts.get(w["lane"], 0) + 1
    scored = [w for w in work_packages if w["lane"] != "canceled"]
    progress = sum(LANE_WEIGHT.get(w["lane"], 0.0) for w in scored) / len(scored) if scored else 0.0

    has_wps = bool(work_packages)
    all_terminal = has_wps and all(w["lane"] in TERMINAL for w in work_packages)
    all_approved = has_wps and all(w["lane"] in TERMINAL | {"approved"} for w in work_packages)
    merged = bool(meta.get("merged_at"))
    done_flags = {
        "specify": "SpecifyCompleted" in phase_seen or "PlanStarted" in phase_seen or has_wps,
        "plan": "PlanCompleted" in phase_seen or "TasksStarted" in phase_seen or has_wps,
        "tasks": "TasksCompleted" in phase_seen or has_wps,
        "implement": all_approved,
        "review": all_terminal,
        "merge": merged,
    }
    labels = {"specify": "Specify", "plan": "Plan", "tasks": "Tasks", "implement": "Implement",
              "review": "Review", "merge": "Merge"}
    steps, current = [], None
    for key in labels:
        state = "done" if done_flags[key] else ("current" if current is None else "todo")
        if state == "current":
            current = key
        steps.append({"id": key, "label": labels[key], "state": state})

    if merged:
        phase = "Merged"
    elif all_terminal:
        phase = "All work packages done"
    elif current:
        phase = labels[current]
    else:
        phase = "Unknown"

    now = datetime.now(timezone.utc)
    active = (has_wps and not all_terminal) or (
        not has_wps and not merged and last_event_at > now - timedelta(days=PLANNING_ACTIVE_DAYS)
    )

    return {
        "generated_at": iso(now),
        "repo": {"name": repo.name, "path": str(repo)},
        "mission": {
            "slug": slug,
            "mission_id": meta.get("mission_id"),
            "mid8": meta.get("mid8"),
            "name": meta.get("friendly_name") or slug,
            "tldr": meta.get("purpose_tldr"),
            "type": meta.get("mission_type"),
            "target_branch": meta.get("target_branch"),
            "merged": merged,
            "accepted": bool(meta.get("accepted_at")),
            "phase": phase,
            "active": active,
        },
        "source": {"mode": src.mode, "detail": src.detail},
        "steps": steps,
        "counts": counts,
        "progress": round(progress, 4),
        "work_packages": work_packages,
        "decisions_open": list(decisions.values()),
        "activity": list(reversed(activity))[:40],
        "last_event_at": iso(last_event_at),
    }


def summarize(board: dict) -> dict:
    wps = board["work_packages"]
    return {
        "mission": board["mission"]["name"],
        "slug": board["mission"]["slug"],
        "phase": board["mission"]["phase"],
        "progress_pct": round(board["progress"] * 100),
        "work_packages": len(wps),
        "lanes": {k: v for k, v in board["counts"].items() if v},
        "open_decisions": [d["question"] for d in board["decisions_open"]],
        "in_flight": [f"{w['id']} {w['title'] or ''}".strip() for w in wps
                      if w["lane"] in ("claimed", "in_progress", "for_review", "in_review")],
        "source": board["source"]["mode"],
    }


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class BoardCache:
    def __init__(self, repo: Path, mission_dir: Path) -> None:
        self.repo = repo
        self.mission_dir = mission_dir
        self.lock = threading.Lock()
        self.board: dict | None = None
        self.fingerprint: str | None = None
        self.checked = 0.0

    def get(self) -> dict:
        with self.lock:
            now = time.monotonic()
            if self.board is not None and now - self.checked < CACHE_TTL_S:
                return self.board
            meta = read_json(self.mission_dir / "meta.json") or {}
            src = MissionSource(self.repo, str(meta.get("mission_slug") or self.mission_dir.name), meta)
            fp = src.mode + "|" + src.fingerprint()
            if self.board is None or fp != self.fingerprint:
                self.board = build_board(self.repo, self.mission_dir)
                self.fingerprint = fp
            else:
                self.board = {**self.board, "generated_at": iso(datetime.now(timezone.utc))}
            self.checked = now
            return self.board


class QuietServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def make_handler(cache: BoardCache, key: str, slug: str, token: str, stop_event: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: Any) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                try:
                    self._send(200, BOARD_HTML.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    self._send(500, b"board.html missing", "text/plain")
            elif path == "/api/board":
                try:
                    self._json(200, cache.get())
                except Exception as exc:  # keep the server alive on bad data
                    self._json(500, {"error": str(exc)})
            elif path == "/api/health":
                self._json(200, {"ok": True, "key": key, "slug": slug, "pid": os.getpid()})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if self.path == "/api/shutdown" and self.headers.get("X-Kitty-Token") == token:
                self._json(200, {"ok": True})
                stop_event.set()
            else:
                self._send(403, b"forbidden", "text/plain")

    return Handler


def board_key(repo: Path, mission_id: str) -> str:
    repo_hash = hashlib.sha1(str(repo.resolve()).lower().encode("utf-8")).hexdigest()[:6]
    return f"{mission_id}-{repo_hash}"


def board_state_path(key: str) -> Path:
    return BOARDS_DIR / f"{key}.json"


def cmd_serve(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    mission_dir = repo / "kitty-specs" / args.mission_dir
    meta = read_json(mission_dir / "meta.json") or {}
    mission_id = str(meta.get("mission_id") or args.mission_dir)
    key = board_key(repo, mission_id)
    cache = BoardCache(repo, mission_dir)
    stop_event = threading.Event()
    server = QuietServer(("127.0.0.1", args.port), make_handler(cache, key, args.mission_dir, args.token, stop_event))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        state = read_json(board_state_path(key)) or {}
        if state.get("pid") == os.getpid():
            board_state_path(key).unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------------------
# start / stop / list / resolve
# ---------------------------------------------------------------------------


def health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(key: str) -> int:
    start = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % PORT_SPAN
    for i in range(PORT_SPAN):
        port = PORT_BASE + (start + i) % PORT_SPAN
        if port_free(port):
            return port
    raise CliError(f"No free port between {PORT_BASE} and {PORT_BASE + PORT_SPAN - 1}.")


def spawn_server(repo: Path, slug: str, port: int, token: str, key: str) -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOGS_DIR / f"{key}.log", "ab")
    cmd = [sys.executable, str(Path(__file__).resolve()), "serve",
           "--repo", str(repo), "--mission-dir", slug, "--port", str(port), "--token", token]
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                              "close_fds": True, "cwd": str(repo)}
    if os.name == "nt":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(cmd, creationflags=base | 0x01000000, **kwargs)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            proc = subprocess.Popen(cmd, creationflags=base, **kwargs)
    else:
        proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
    return proc.pid


def live_boards() -> list[dict]:
    boards = []
    if not BOARDS_DIR.is_dir():
        return boards
    for path in sorted(BOARDS_DIR.glob("*.json")):
        state = read_json(path)
        if not isinstance(state, dict):
            path.unlink(missing_ok=True)
            continue
        h = health(int(state.get("port", 0)))
        if h and state.get("key") and h.get("key") == state.get("key"):
            boards.append(state)
        else:
            path.unlink(missing_ok=True)
    return boards


def cmd_start(args: argparse.Namespace) -> int:
    repo = find_repo(args.repo)
    if args.mission:
        mission_dir = match_mission(repo, args.mission)
    else:
        active = [b for b in (build_board(repo, d, use_git=False) for d in list_missions(repo)) if b["mission"]["active"]]
        if len(active) != 1:
            raise CliError(
                "Couldn't pick a mission automatically; pass --mission.",
                candidates=[b["mission"]["slug"] for b in active] or [d.name for d in list_missions(repo)][-20:],
            )
        mission_dir = repo / "kitty-specs" / active[0]["mission"]["slug"]

    meta = read_json(mission_dir / "meta.json") or {}
    mission_id = str(meta.get("mission_id") or mission_dir.name)
    key = board_key(repo, mission_id)
    board = build_board(repo, mission_dir)

    state = read_json(board_state_path(key))
    reused = False
    if isinstance(state, dict):
        h = health(int(state.get("port", 0)))
        reused = bool(h and h.get("key") == key)
    if not reused:
        port = pick_port(key)
        token = secrets.token_hex(16)
        pid = spawn_server(repo, mission_dir.name, port, token, key)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            h = health(port)
            if h and h.get("key") == key:
                break
            time.sleep(0.25)
        else:
            raise CliError(f"The board server didn't come up on port {port}. See {LOGS_DIR / (key + '.log')}.")
        state = {"key": key, "mission_id": mission_id, "slug": mission_dir.name, "repo": str(repo), "port": port,
                 "pid": h.get("pid", pid), "token": token, "url": f"http://127.0.0.1:{port}/",
                 "started_at": iso(datetime.now(timezone.utc))}
        BOARDS_DIR.mkdir(parents=True, exist_ok=True)
        board_state_path(key).write_text(json.dumps(state, indent=2), encoding="utf-8")

    if not args.no_open:
        webbrowser.open(state["url"])
    emit({"ok": True, "url": state["url"], "reused": reused, "port": state["port"], "summary": summarize(board)})
    return 0


def stop_board(state: dict) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{state['port']}/api/shutdown", method="POST",
                                     headers={"X-Kitty-Token": state.get("token", "")}, data=b"")
        urllib.request.urlopen(req, timeout=3).read()
    except (urllib.error.URLError, OSError):
        pass
    for _ in range(20):
        if not health(int(state["port"])):
            break
        time.sleep(0.2)
    else:
        pid = int(state.get("pid", 0))
        if pid and os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=NO_WINDOW)
        elif pid:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    board_state_path(str(state["key"])).unlink(missing_ok=True)
    return not health(int(state["port"]))


def cmd_stop(args: argparse.Namespace) -> int:
    boards = live_boards()
    if args.all:
        targets = boards
    else:
        if not args.mission:
            raise CliError("Pass --mission (or --all).", running=[b["slug"] for b in boards])
        repo = find_repo(args.repo)
        mission_dir = match_mission(repo, args.mission)
        meta = read_json(mission_dir / "meta.json") or {}
        key = board_key(repo, str(meta.get("mission_id") or mission_dir.name))
        targets = [b for b in boards if b["key"] == key]
        if not targets:
            emit({"ok": True, "stopped": [], "message": f"No board is running for {mission_dir.name}."})
            return 0
    stopped = [b["slug"] for b in targets if stop_board(b)]
    emit({"ok": True, "stopped": stopped})
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    emit({"ok": True, "boards": [{k: b[k] for k in ("slug", "repo", "url", "started_at") if k in b} for b in live_boards()]})
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    repo = find_repo(args.repo)
    rows = []
    for d in list_missions(repo):
        b = build_board(repo, d, use_git=False)
        rows.append({
            "slug": b["mission"]["slug"],
            "name": b["mission"]["name"],
            "active": b["mission"]["active"],
            "phase": b["mission"]["phase"],
            "last_event_at": b["last_event_at"],
            "lanes": {k: v for k, v in b["counts"].items() if v},
        })
    rows.sort(key=lambda r: (not r["active"], -(parse_ts(r["last_event_at"]).timestamp())))
    emit({"ok": True, "repo": str(repo), "missions": rows[:20]})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kitty_dashboard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--mission")
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--mission")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("resolve")
    p.add_argument("--repo", default=os.getcwd())
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("serve")
    p.add_argument("--repo", required=True)
    p.add_argument("--mission-dir", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--token", required=True)
    p.set_defaults(func=cmd_serve)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        emit({"ok": False, "error": str(exc), **exc.extra})
        return 1


if __name__ == "__main__":
    sys.exit(main())

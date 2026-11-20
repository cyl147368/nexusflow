"""Small GitHub updater running beside the deployment checkout."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.getenv("NEXUSFLOW_UPDATER_TOKEN", "")
REPO = os.getenv("UPDATER_REPO_PATH", "/opt/nexusflow")
STATE_FILE = os.getenv("UPDATER_STATE_FILE", "/app/state/update-state.json")
BRANCH = os.getenv("UPDATER_BRANCH", "main")
SERVICES = ["nexusflow", "icloud-prime", "mail-hub", "payment-detector"]
_lock = threading.Lock()


def _run(args, timeout=120):
    result = subprocess.run(
        args,
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return {
        "ok": result.returncode == 0,
        "code": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


def _commit(ref="HEAD"):
    result = _run(["git", "rev-parse", ref], timeout=20)
    return result["stdout"].strip() if result["ok"] else ""


def _state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {"status": "idle", "message": "尚未执行更新"}


def _save_state(**updates):
    state = _state()
    state.update(updates)
    state["updated_at"] = time.time()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, STATE_FILE)
    return state


def check_update() -> dict:
    fetch = _run(["git", "fetch", "origin", BRANCH], timeout=120)
    current = _commit("HEAD")
    remote = _commit(f"origin/{BRANCH}") if fetch["ok"] else ""
    diff = _run(["git", "diff", "--name-only", f"HEAD..origin/{BRANCH}"], timeout=30)
    changed = [line for line in diff["stdout"].splitlines() if line.strip()]
    return {
        "ok": fetch["ok"] and bool(current) and bool(remote),
        "branch": BRANCH,
        "current": current,
        "remote": remote,
        "behind": bool(current and remote and current != remote),
        "changed_files": changed,
        "fetch_error": fetch["stderr"] if not fetch["ok"] else "",
    }


def apply_update():
    if not _lock.acquire(blocking=False):
        return False, "已有更新任务在执行"

    def worker():
        old = _commit("HEAD")
        try:
            _save_state(status="running", stage="fetch", message="正在从 GitHub 拉取更新", old=old)
            fetched = _run(["git", "fetch", "origin", BRANCH], timeout=180)
            if not fetched["ok"]:
                raise RuntimeError(fetched["stderr"] or "git fetch 失败")
            latest = _commit(f"origin/{BRANCH}")
            _save_state(stage="checkout", remote=latest, message=f"正在更新到 {latest[:12]}")
            reset = _run(["git", "reset", "--hard", f"origin/{BRANCH}"], timeout=120)
            if not reset["ok"]:
                raise RuntimeError(reset["stderr"] or "git reset 失败")
            _save_state(stage="build", message="正在构建新镜像")
            built = _run(["docker", "compose", "build", *SERVICES], timeout=1800)
            if not built["ok"]:
                raise RuntimeError(built["stderr"] or "docker compose build 失败")
            _save_state(stage="restart", message="正在滚动重启项目容器")
            started = _run(
                ["docker", "compose", "up", "-d", "--no-deps", *SERVICES],
                timeout=600,
            )
            if not started["ok"]:
                raise RuntimeError(started["stderr"] or "docker compose up 失败")
            _save_state(
                status="completed",
                stage="completed",
                message="更新完成",
                old=old,
                current=latest,
                finished_at=time.time(),
            )
        except Exception as exc:
            _save_state(
                status="failed",
                stage="failed",
                message=str(exc)[:1000],
                finished_at=time.time(),
            )
        finally:
            _lock.release()

    threading.Thread(target=worker, daemon=True, name="nexusflow-update").start()
    return True, "更新任务已启动"


class Handler(BaseHTTPRequestHandler):
    def _auth(self):
        return bool(TOKEN) and self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._auth():
            return self._json(401, {"ok": False, "message": "未授权"})
        if self.path == "/api/health":
            return self._json(200, {"ok": True, "service": "nexusflow-updater", "repo": REPO})
        if self.path == "/api/update/check":
            return self._json(200, {"ok": True, **check_update()})
        if self.path == "/api/update/status":
            return self._json(200, {"ok": True, "state": _state()})
        return self._json(404, {"ok": False, "message": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._json(401, {"ok": False, "message": "未授权"})
        if self.path == "/api/update/apply":
            started, message = apply_update()
            return self._json(200 if started else 409, {"ok": started, "message": message, "state": _state()})
        return self._json(404, {"ok": False, "message": "not found"})

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8092), Handler).serve_forever()

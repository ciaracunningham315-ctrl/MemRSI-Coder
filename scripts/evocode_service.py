"""Start, inspect, and stop a local EvoCode workbench."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from evocode.config import load_config

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
COMPOSE = ("docker", "compose")
HTTP = build_opener(ProxyHandler({}))


def read_state(state_file):
    try:
        return json.loads(state_file.read_text())
    except FileNotFoundError:
        return {}


def process_matches(state):
    pid = state.get("pid")
    token = state.get("token")
    if not isinstance(pid, int) or pid <= 1 or not token:
        return False
    result = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "command="], check=False, capture_output=True, text=True,
    )
    return result.returncode == 0 and "evocode_service.py" in result.stdout and token in result.stdout.split()


def ready(url):
    try:
        with HTTP.open(url, timeout=1) as response:
            return response.status == 200
    except (URLError, TimeoutError):
        return False


def wait_ready(url, timeout, keep_waiting):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and keep_waiting():
        if ready(url):
            return True
        time.sleep(0.2)
    return False


def compose_container():
    return subprocess.check_output(
        [*COMPOSE, "ps", "--status", "running", "-q", "qdrant"], text=True, timeout=10,
    ).strip()


def supervise(args, config, state_file):
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())

    state = {"pid": os.getpid(), "token": args.token, "port": args.port}

    def publish(status, **details):
        temp = state_file.with_suffix(".tmp")
        temp.write_text(json.dumps({**state, "status": status, **details}) + "\n")
        temp.replace(state_file)

    web = None
    started_qdrant = False
    owned_container = ""
    error = None
    publish("starting")
    try:
        # Reserve the listener before touching dependencies and pass the same
        # socket to Uvicorn. A second workbench cannot claim an existing port.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", args.port))
            listener.listen()
            qdrant_port = int(os.environ.get("EVOCODE_QDRANT_PORT", "6333"))
            local_qdrant = {
                f"http://{host}:{qdrant_port}" for host in ("127.0.0.1", "localhost")
            }
            uses_memory = config.raw_rag.enabled or (
                config.raw_rag.background_indexing and config.raw_rag.index_when_disabled
            )
            if uses_memory and config.raw_rag.qdrant_url.rstrip("/") in local_qdrant:
                qdrant_health = f"http://127.0.0.1:{qdrant_port}/healthz"
                if not ready(qdrant_health):
                    started_qdrant = not compose_container()
                    if started_qdrant:
                        subprocess.run([*COMPOSE, "up", "-d", "qdrant"], check=True, timeout=45)
                        owned_container = compose_container()
                    if not wait_ready(qdrant_health, 15, lambda: not stopping.is_set()):
                        raise RuntimeError("本机 Qdrant 未通过健康检查")
            if stopping.is_set():
                return
            fd = listener.fileno()
            web = subprocess.Popen([
                sys.executable, "-m", "uvicorn", "evocode.api:create_app", "--factory",
                "--fd", str(fd), "--timeout-graceful-shutdown", "5", "--no-access-log",
            ], pass_fds=(fd,))
        url = f"http://127.0.0.1:{args.port}/api/health"
        if not wait_ready(url, 15, lambda: web.poll() is None and not stopping.is_set()):
            raise RuntimeError("Web/API 未通过健康检查")
        publish("running", web_pid=web.pid, qdrant_started=bool(owned_container))
        while web.poll() is None and not stopping.wait(0.25):
            pass
        if not stopping.is_set():
            raise RuntimeError(f"Web/API 已退出，退出码 {web.returncode}")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        error = str(exc)
        print(error, file=sys.stderr, flush=True)
    finally:
        if web is not None and web.poll() is None:
            web.terminate()
            try:
                web.wait(timeout=20)
            except subprocess.TimeoutExpired:
                web.kill()
                web.wait()
                error = error or "Web/API 未能在 20 秒内完成正常关闭"
        if started_qdrant:
            try:
                # Clean up a container created even if compose up timed out.
                owned_container = owned_container or compose_container()
                if owned_container:
                    subprocess.run(["docker", "stop", owned_container], check=True, timeout=20)
            except (OSError, subprocess.SubprocessError) as exc:
                error = error or f"Qdrant 关闭失败：{exc}"
        publish("failed" if error else "stopped", error=error)


def start(args, state_file, log_file, lock_file):
    with lock_file.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("此数据目录已有运行中的 EvoCode 服务，请使用 status 查看") from None
        if args.build or not (WEB / "dist/index.html").is_file():
            if not (WEB / "node_modules/.bin/vite").is_file():
                subprocess.run(["npm", "ci"], cwd=WEB, check=True)
            subprocess.run(["npm", "run", "build"], cwd=WEB, check=True)
        token = uuid.uuid4().hex
        # The detached supervisor inherits the lock. It releases the lock when
        # it exits, including on signals or an unexpected process failure.
        with log_file.open("ab", buffering=0) as log:
            worker = subprocess.Popen([
                sys.executable, str(Path(__file__).resolve()), "supervise",
                "--port", str(args.port), "--token", token, "--lock-fd", str(lock.fileno()),
            ], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                pass_fds=(lock.fileno(),))
    deadline = time.monotonic() + 80
    while time.monotonic() < deadline:
        state = read_state(state_file)
        if state.get("token") == token and state["status"] == "running":
            print(f"EvoCode http://127.0.0.1:{args.port}/")
            return
        if worker.poll() is not None:
            break
        time.sleep(0.2)
    if worker.poll() is None:
        worker.terminate()
    raise RuntimeError(f"EvoCode 未启动，请查看 {log_file}")


def stop(state_file):
    state = read_state(state_file)
    if not process_matches(state):
        print("没有由此入口管理的 EvoCode 服务")
        return
    try:
        os.kill(state["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline and process_matches(state):
        time.sleep(0.25)
    if process_matches(state):
        raise RuntimeError("EvoCode 尚在关闭中，请用 status 确认")
    result = read_state(state_file)
    if result.get("token") != state["token"] or result["status"] != "stopped":
        raise RuntimeError(result.get("error") or "未确认 EvoCode 正常关闭")
    print("EvoCode 已关闭")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop", "supervise"))
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    os.chdir(ROOT)
    config = load_config()
    ops = Path(config.data_dir).resolve() / "workbench"
    ops.mkdir(parents=True, exist_ok=True)
    state_file = ops / "service.json"
    log_file = ops / "service.log"
    lock_file = ops / "service.lock"
    try:
        if args.action == "supervise":
            with os.fdopen(args.lock_fd, "a") as lock:
                if not args.token or not os.path.samestat(os.fstat(lock.fileno()), lock_file.stat()):
                    raise RuntimeError("Invalid supervisor lock")
                supervise(args, config, state_file)
        elif args.action == "start":
            start(args, state_file, log_file, lock_file)
        elif args.action == "stop":
            stop(state_file)
        else:
            state = read_state(state_file)
            if not process_matches(state):
                print(state.get("error") or "EvoCode 未运行")
                return 1
            url = f"http://127.0.0.1:{state['port']}/"
            print(f"EvoCode {state['status']} {url}")
            return 0 if state["status"] == "running" and ready(url + "api/health") else 1
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

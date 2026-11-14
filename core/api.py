# -*- coding: utf-8 -*-
"""本地 HTTP 服务：静态页面 + 应用 API"""
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .catalog import CATALOG
from .config import APP_DIR, DATA_DIR, RESOURCE_DIR, Config, read_version
from .hardware import gpu_summary
from .ollama import SERVE_LOG, friendly_error
from .search import build_material, is_chitchat, search_web
from . import updater
from . import vram

WEB_DIR = (RESOURCE_DIR / "web").resolve()
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------- 记忆管理 ----------
def memory_dir(cfg) -> Path:
    raw = str(cfg.get("memoryDir") or "").strip()
    if not raw:
        raw = str(DATA_DIR / "memories")
    return Path(raw)


def list_memories(cfg):
    d = memory_dir(cfg)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []
    out = []
    try:
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []
    for f in files:
        try:
            out.append(
                {
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                }
            )
        except Exception:
            continue
    return out


def read_memory(cfg, name: str) -> str:
    safe = _safe_memory_name(name)
    if not safe:
        return ""
    f = memory_dir(cfg) / safe
    if not f.is_file():
        return ""
    try:
        return f.read_text(encoding="utf-8")
    except Exception:
        return ""


def write_memory(cfg, name: str, content: str):
    safe = _safe_memory_name(name)
    if not safe:
        return
    d = memory_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / safe).write_text(content or "", encoding="utf-8")


def delete_memory(cfg, name: str):
    safe = _safe_memory_name(name)
    if not safe:
        return
    f = memory_dir(cfg) / safe
    if f.is_file():
        try:
            f.unlink()
        except Exception:
            pass


def _safe_memory_name(name: str) -> str:
    """把用户输入的记忆名清洗成安全的 .md 文件名。"""
    name = (name or "").strip()
    if not name:
        return ""
    if not name.lower().endswith(".md"):
        name = name + ".md"
    # 去掉路径分隔符和非法字符
    name = name.replace("\\", "_").replace("/", "_")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    if not name or name == ".md":
        return ""
    return name


class AppContext:
    def __init__(self, cfg: Config, hub, manager):
        self.cfg = cfg
        self.hub = hub
        self.manager = manager
        self.version = read_version()
        self.quit_event = threading.Event()
        self.window = None  # 原生窗口（pywebview），由 app.py 注入
        # 本机接口令牌：只有本应用页面拿得到，防止其它程序随意调用
        self.token = secrets.token_urlsafe(24)
        self._hardware = None
        self._update_lock = threading.Lock()

    def hardware(self) -> dict:
        if self._hardware is None:
            try:
                self._hardware = gpu_summary()
            except Exception:
                self._hardware = {"gpus": [], "maxVramGB": 0}
        return self._hardware

    def close_window(self):
        """关闭原生窗口；没有窗口时只设置退出标记。"""
        win = self.window
        if win is not None:
            try:
                win.destroy()
                return
            except Exception:
                pass
        self.quit_event.set()


def build_state(app: AppContext) -> dict:
    st = app.manager.status()
    return {
        "ok": True,
        "version": app.version,
        "config": {
            "modelDir": app.cfg.get("modelDir"),
            "intelMode": bool(app.cfg.get("intelMode")),
            "numCtx": app.cfg.get("numCtx"),
            "panelPort": app.cfg.get("panelPort"),
            "ollamaPort": app.cfg.get("ollamaPort"),
            "releaseOnSwitch": bool(app.cfg.get("releaseOnSwitch")),
            "releaseOnStop": bool(app.cfg.get("releaseOnStop")),
            "restartOnSwitch": bool(app.cfg.get("restartOnSwitch")),
            "ollamaPath": str(app.cfg.get("ollamaPath") or ""),
            "memoryDir": app.cfg.get("memoryDir"),
            "autoMemory": bool(app.cfg.get("autoMemory")),
            "checkUpdate": bool(app.cfg.get("checkUpdate")),
            "firstRunDone": bool(app.cfg.get("firstRunDone")),
        },
        "ollama": st,
        "ollamaCandidates": [
            {"path": str(p), "kind": k} for p, k in app.manager._candidates()
        ],
        "models": app.manager.list_models() if st["installed"] else [],
        "memories": list_memories(app.cfg),
        "tasks": dict(app.hub.state),
        "catalog": CATALOG,
        "hardware": app.hardware(),
        "running": st["serveRunning"],
        "logPath": str(SERVE_LOG) if SERVE_LOG.exists() else "",
    }


def pick_dialog(kind: str) -> str:
    out = Path(tempfile.gettempdir()) / f"ds_launcher_pick_{uuid.uuid4().hex}.txt"
    out_posix = out.as_posix()
    if kind == "file":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "$owner = New-Object System.Windows.Forms.Form\n"
            "$owner.TopMost = $true\n"
            "$owner.WindowState = 'Minimized'\n"
            "$owner.ShowInTaskbar = $false\n"
            "$owner.Show()\n"
            "$d = New-Object System.Windows.Forms.OpenFileDialog\n"
            '$d.Filter = "ollama.exe|ollama.exe|可执行文件 (*.exe)|*.exe"\n'
            '$d.Title = "选择已安装的 ollama.exe"\n'
            'if ($d.ShowDialog($owner) -eq "OK") { [System.IO.File]::WriteAllText("__OUT__", $d.FileName) }\n'
            "$owner.Dispose()\n"
        )
    else:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "$owner = New-Object System.Windows.Forms.Form\n"
            "$owner.TopMost = $true\n"
            "$owner.WindowState = 'Minimized'\n"
            "$owner.ShowInTaskbar = $false\n"
            "$owner.Show()\n"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog\n"
            '$d.Description = "选择模型存放目录"\n'
            "$d.ShowNewFolderButton = $true\n"
            'if ($d.ShowDialog($owner) -eq "OK") { [System.IO.File]::WriteAllText("__OUT__", $d.SelectedPath) }\n'
            "$owner.Dispose()\n"
        )
    script = script.replace("__OUT__", out_posix)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            timeout=600,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return ""
    try:
        if out.exists():
            value = out.read_text(encoding="utf-8").strip()
            out.unlink(missing_ok=True)
            return value
    except Exception:
        pass
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "DeepSeekLauncher"

    @property
    def app(self) -> AppContext:
        return self.server.app

    def log_message(self, fmt, *args):
        pass

    # ---------- 工具 ----------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _fail(self, msg, code=400):
        self._send_json({"ok": False, "error": str(msg)}, code)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/state":
                return self._send_json(build_state(self.app))
            if path == "/api/models":
                return self._send_json(
                    {"ok": True, "models": self.app.manager.list_models()}
                )
            if path == "/api/vram":
                return self._send_json({"ok": True, **vram.usage()})
            if path == "/api/fs":
                return self._fs(query)
            if path == "/api/memories":
                return self._send_json({"ok": True, "memories": list_memories(self.app.cfg)})
            if path == "/api/events":
                return self._events()
            if path.startswith("/api/"):
                return self._fail("未知接口", 404)
            return self._static(path)
        except Exception as e:
            try:
                self._fail(friendly_error(e), 500)
            except Exception:
                pass

    def _fs(self, query):
        raw = (query.get("path") or [""])[0].strip()
        drives = []
        if os.name == "nt":
            import string
            for letter in string.ascii_uppercase:
                root = f"{letter}:\\"
                if os.path.exists(root):
                    drives.append({"name": f"{letter}:", "path": root})
        if not raw:
            raw = drives[0]["path"] if drives else str(Path.home())
        p = Path(raw)
        dirs = []
        error = ""
        try:
            p = p.resolve()
            children = sorted(p.iterdir(), key=lambda it: it.name.lower())
            for child in children:
                try:
                    if child.is_dir():
                        dirs.append({"name": child.name, "path": str(child)})
                except OSError:
                    continue
        except PermissionError:
            error = "没有访问该目录的权限"
        except FileNotFoundError:
            error = "目录不存在"
        except OSError:
            error = "无法读取该目录"
        parent = "" if p.parent == p else str(p.parent)
        free = total = 0
        try:
            usage = shutil.disk_usage(str(p))
            free, total = usage.free, usage.total
        except Exception:
            pass
        self._send_json(
            {
                "ok": True,
                "path": str(p),
                "parent": parent,
                "dirs": dirs,
                "drives": drives,
                "free": free,
                "total": total,
                "error": error,
            }
        )

    def _static(self, path: str):
        rel = urllib.parse.unquote(path.lstrip("/")) or "index.html"
        target = (WEB_DIR / rel).resolve()
        if target != WEB_DIR and WEB_DIR not in target.parents:
            return self._fail("非法路径", 403)
        if not target.is_file():
            return self._fail("文件不存在", 404)
        data = target.read_bytes()
        if target.name == "index.html":
            # 把本机接口令牌写进页面，前端请求时带回
            data = data.replace(b"__DS_TOKEN_VALUE__", self.app.token.encode("utf-8"))
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _events(self):
        hub = self.app.hub
        q = hub.subscribe()
        self._start_sse()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            hub.unsubscribe(q)

    # ---------- POST ----------
    def _same_origin(self) -> bool:
        """只接受本应用页面发起的请求，挡住其它网页的跨站提交。"""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True  # 非浏览器来源（命令行/本机脚本）
        try:
            port = self.server.server_address[1]
        except Exception:
            port = 0
        return origin in (
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        )

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if not self._same_origin():
                return self._fail("非法请求来源", 403)
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/json" not in ctype:
                return self._fail("请求格式不正确", 415)
            if self.headers.get("X-DS-Token") != self.app.token:
                return self._fail("请求令牌无效，请刷新页面后重试", 403)
            if path == "/api/chat":
                return self._chat()
            body = self._read_json()
            if path == "/api/update/check":
                return self._update_check()
            if path == "/api/update/install":
                return self._update_install(body)
            if path == "/api/config":
                return self._config(body)
            if path == "/api/memories":
                return self._memories(body)
            if path == "/api/pick":
                value = pick_dialog(body.get("kind") or "dir")
                if not value:
                    return self._send_json({"ok": False, "canceled": True})
                return self._send_json({"ok": True, "path": value})
            if path == "/api/open":
                return self._open(body)
            if path == "/api/mkdir":
                return self._mkdir(body)
            if path == "/api/ollama/install":
                mode = body.get("mode") or "standard"
                if mode not in ("intel", "standard"):
                    return self._fail("未知安装模式")
                threading.Thread(
                    target=self.app.manager.install, args=(mode,), daemon=True
                ).start()
                return self._send_json({"ok": True})
            if path == "/api/ollama/start":
                threading.Thread(
                    target=self._safe_start, daemon=True
                ).start()
                return self._send_json({"ok": True})
            if path == "/api/ollama/stop":
                self.app.manager.stop()
                return self._send_json({"ok": True})
            if path == "/api/ollama/restart":
                threading.Thread(
                    target=self._safe_restart, daemon=True
                ).start()
                return self._send_json({"ok": True})
            if path == "/api/ollama/use":
                exe = str(body.get("path") or "").strip()
                if not exe:
                    # 传空路径表示恢复使用内置/自动检测的版本
                    self.app.cfg.update(ollamaPath="")
                    self.app.manager.stop()
                    self.app.manager.detect()
                    threading.Thread(target=self._safe_start, daemon=True).start()
                    return self._send_json({"ok": True})
                if not Path(exe).is_file():
                    return self._fail("路径无效，请选择 ollama.exe")
                kind = self.app.manager._guess_kind(Path(exe))
                self.app.cfg.update(ollamaPath=exe, intelMode=(kind == "intel"))
                self.app.manager.stop()
                self.app.manager.detect()
                threading.Thread(target=self._safe_start, daemon=True).start()
                return self._send_json({"ok": True})
            if path == "/api/models/pull":
                model = str(body.get("model") or "").strip()
                if not model:
                    return self._fail("请选择模型")
                threading.Thread(
                    target=self._safe_pull, args=(model,), daemon=True
                ).start()
                return self._send_json({"ok": True})
            if path == "/api/models/delete":
                model = str(body.get("model") or "").strip()
                if not model:
                    return self._fail("缺少模型名")
                self.app.manager.delete_model(model)
                return self._send_json({"ok": True})
            if path == "/api/models/load":
                model = str(body.get("model") or "").strip()
                if not model:
                    return self._fail("缺少模型名")
                threading.Thread(
                    target=self._safe_load, args=(model,), daemon=True
                ).start()
                return self._send_json({"ok": True})
            if path == "/api/quit":
                self._send_json({"ok": True})
                self.app.quit_event.set()
                # 原生窗口模式下需要显式关闭窗口，随后由主流程统一清理资源
                threading.Thread(target=self.app.close_window, daemon=True).start()
                return
            return self._fail("未知接口", 404)
        except Exception as e:
            try:
                self._fail(friendly_error(e), 500)
            except Exception:
                pass

    def _mkdir(self, body):
        target = str(body.get("path") or "").strip()
        if not target:
            return self._fail("请输入文件夹名称")
        try:
            Path(target).mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return self._send_json({"ok": True, "path": target})
        except Exception as e:
            return self._fail(friendly_error(e))
        return self._send_json({"ok": True, "path": target})

    def _safe_start(self):
        try:
            self.app.manager.start()
        except Exception:
            pass

    def _safe_restart(self):
        try:
            self.app.manager.restart()
        except Exception:
            pass

    def _safe_pull(self, model):
        try:
            self.app.manager.pull(model)
        except Exception:
            pass

    def _safe_load(self, model):
        try:
            self.app.manager.load_model(model)
            self.app.hub.publish(
                "model",
                {
                    "status": "loaded",
                    "percent": 100,
                    "model": model,
                    "message": f"{model} 已加载到显存/内存",
                },
            )
        except Exception as e:
            self.app.hub.publish(
                "model",
                {"status": "error", "percent": 0, "model": model, "message": str(e)},
            )

    def _config(self, body):
        cfg = self.app.cfg
        changed = []
        need_restart = False
        if "modelDir" in body:
            value = str(body.get("modelDir") or "").strip()
            if not value:
                return self._fail("目录不能为空")
            try:
                Path(value).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return self._fail(f"无法创建目录：{e}")
            if value != cfg.get("modelDir"):
                cfg.update(modelDir=value)
                changed.append("modelDir")
                need_restart = True
        if "intelMode" in body:
            value = bool(body.get("intelMode"))
            if value != bool(cfg.get("intelMode")):
                cfg.update(intelMode=value)
                changed.append("intelMode")
                need_restart = True
        if "numCtx" in body:
            try:
                value = max(2048, int(body.get("numCtx")))
            except Exception:
                return self._fail("上下文长度无效")
            if value != int(cfg.get("numCtx") or 8192):
                cfg.update(numCtx=value)
                changed.append("numCtx")
                need_restart = True
        if "releaseOnSwitch" in body:
            value = bool(body.get("releaseOnSwitch"))
            if value != bool(cfg.get("releaseOnSwitch")):
                cfg.update(releaseOnSwitch=value)
                changed.append("releaseOnSwitch")
        if "releaseOnStop" in body:
            value = bool(body.get("releaseOnStop"))
            if value != bool(cfg.get("releaseOnStop")):
                cfg.update(releaseOnStop=value)
                changed.append("releaseOnStop")
        if "restartOnSwitch" in body:
            value = bool(body.get("restartOnSwitch"))
            if value != bool(cfg.get("restartOnSwitch")):
                cfg.update(restartOnSwitch=value)
                changed.append("restartOnSwitch")
        if "autoMemory" in body:
            value = bool(body.get("autoMemory"))
            if value != bool(cfg.get("autoMemory")):
                cfg.update(autoMemory=value)
                changed.append("autoMemory")
        if "memoryDir" in body:
            value = str(body.get("memoryDir") or "").strip()
            if not value:
                return self._fail("记忆目录不能为空")
            try:
                Path(value).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return self._fail(f"无法创建目录：{e}")
            if value != cfg.get("memoryDir"):
                cfg.update(memoryDir=value)
                changed.append("memoryDir")
        if "checkUpdate" in body:
            value = bool(body.get("checkUpdate"))
            if value != bool(cfg.get("checkUpdate")):
                cfg.update(checkUpdate=value)
                changed.append("checkUpdate")
        if "firstRunDone" in body:
            value = bool(body.get("firstRunDone"))
            if value != bool(cfg.get("firstRunDone")):
                cfg.update(firstRunDone=value)
                changed.append("firstRunDone")
        if need_restart:
            self.app.manager.detect()
            if self.app.manager.status()["installed"]:
                self.app.manager.stop()
                threading.Thread(target=self._safe_start, daemon=True).start()
        return self._send_json({"ok": True, "config": build_state(self.app)["config"]})

    # ---------- 在线更新 ----------
    def _update_check(self):
        try:
            info = updater.latest_release()
        except Exception as e:
            return self._fail(friendly_error(e))
        current = self.app.version
        has_update = updater.parse_version(info["version"]) > updater.parse_version(current)
        return self._send_json(
            {
                "ok": True,
                "current": current,
                "latest": info["version"],
                "tag": info["tag"],
                "hasUpdate": has_update,
                "url": info["url"],
                "notes": (info.get("body") or "")[:2000],
            }
        )

    def _update_install(self, body):
        version = str(body.get("version") or "").strip().lstrip("vV")
        if not re.fullmatch(r"\d+(\.\d+){1,3}", version):
            return self._fail("版本号无效")
        if not self.app._update_lock.acquire(blocking=False):
            return self._fail("已有更新任务在进行")
        threading.Thread(target=self._run_update, args=(version,), daemon=True).start()
        return self._send_json({"ok": True})

    def _run_update(self, version: str):
        try:
            dest = Path(tempfile.gettempdir()) / updater.installer_name(version)
            last = [0.0]

            def on_progress(done, total, speed):
                now = time.time()
                if now - last[0] < 0.4 and done != total:
                    return
                last[0] = now
                percent = int(done * 100 / total) if total else 0
                self.app.hub.publish(
                    "update",
                    {
                        "status": "downloading",
                        "percent": percent,
                        "message": updater.progress_message(done, total, speed),
                    },
                )

            self.app.hub.publish(
                "update",
                {"status": "downloading", "percent": 0, "message": "正在连接国内下载节点…"},
            )
            node = updater.download_installer(version, dest, on_progress)
            self.app.hub.publish(
                "update",
                {
                    "status": "installing",
                    "percent": 100,
                    "message": "下载完成（"
                    + ("国内加速节点" if node != "official" else "GitHub 官方")
                    + "），正在启动安装程序…",
                },
            )
            subprocess.Popen([str(dest)])
            time.sleep(1.0)
            self.app.quit_event.set()
            threading.Thread(target=self.app.close_window, daemon=True).start()
        except Exception as e:
            self.app.hub.publish(
                "update",
                {"status": "error", "percent": 0, "message": friendly_error(e)},
            )
        finally:
            try:
                self.app._update_lock.release()
            except Exception:
                pass

    def _memories(self, body):
        action = str(body.get("action") or "list").strip()
        name = str(body.get("name") or "").strip()
        cfg = self.app.cfg
        if action == "list":
            return self._send_json({"ok": True, "memories": list_memories(cfg)})
        if action == "read":
            if not name:
                return self._fail("缺少记忆名称")
            return self._send_json(
                {"ok": True, "name": name, "content": read_memory(cfg, name)}
            )
        if action == "create":
            if not name:
                return self._fail("请输入记忆名称")
            name = _safe_memory_name(name)
            if not name:
                return self._fail("记忆名称无效")
            write_memory(cfg, name, str(body.get("content") or ""))
            return self._send_json({"ok": True, "name": name, "memories": list_memories(cfg)})
        if action == "update":
            if not name:
                return self._fail("缺少记忆名称")
            write_memory(cfg, name, str(body.get("content") or ""))
            return self._send_json({"ok": True, "name": name})
        if action == "delete":
            if not name:
                return self._fail("缺少记忆名称")
            delete_memory(cfg, name)
            return self._send_json({"ok": True, "memories": list_memories(cfg)})
        return self._fail("未知操作")

    def _open(self, body):
        target = str(body.get("path") or self.app.cfg.get("modelDir") or "")
        if not target:
            return self._fail("路径为空")
        p = Path(target)
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return self._fail(f"路径不存在：{e}")
        try:
            os.startfile(str(p))  # noqa: S606 - Windows 打开资源管理器
        except Exception as e:
            return self._fail(friendly_error(e))
        return self._send_json({"ok": True})

    def _chat(self):
        body = self._read_json()
        model = str(body.get("model") or "").strip()
        raw_messages = body.get("messages") or []
        messages = [
            m for m in raw_messages
            if isinstance(m, dict) and m.get("role") and isinstance(m.get("content"), str)
        ]
        if not model:
            return self._fail("请先选择模型")
        if not messages:
            return self._fail("消息不能为空")

        level = str(body.get("thinkLevel") or "normal")
        if level not in ("off", "normal", "deep"):
            level = "normal"
        want_web = bool(body.get("webSearch"))
        memory_name = str(body.get("memory") or "").strip()
        query = ""
        for item in reversed(messages):
            if item.get("role") == "user":
                query = item["content"].strip()
                break

        mgr = self.app.manager
        switching = bool(mgr._last_chat_model) and mgr._last_chat_model != model
        if not switching:
            try:
                mgr.ensure_running()
            except Exception as e:
                return self._fail(f"Ollama 服务未就绪：{friendly_error(e)}", 503)
            self._start_sse()
        else:
            self._start_sse()
            try:
                if bool(self.app.cfg.get("restartOnSwitch")):
                    # 切换模型：先卸载旧模型，再重启 Ollama，彻底释放显存
                    self._sse_event(
                        {"status": "restarting", "message": "正在重启 Ollama 以释放显存…"}
                    )
                    try:
                        mgr.unload_all()
                    except Exception:
                        pass
                    mgr.restart()
                else:
                    mgr.ensure_running()
                    if bool(self.app.cfg.get("releaseOnSwitch")):
                        mgr.unload_all()
            except Exception as e:
                self._sse_event({"error": f"Ollama 服务重启失败：{friendly_error(e)}"})
                return
        mgr._last_chat_model = model
        mgr._loaded_model = model

        sources = []
        # 关闭「联网搜索」后完全不联网；开启后每轮都会联网检索（纯寒暄除外）
        need_search = bool(query) and want_web and not is_chitchat(query)
        if need_search:
            self._sse_event({"status": "searching", "message": "正在联网搜索…"})
            try:
                sources = search_web(query)
            except Exception as e:
                sources = []
                self._sse_event({"notice": f"联网搜索失败：{friendly_error(e)}"})
            if sources:
                self._sse_event({"sources": sources})
                self._sse_event(
                    {"notice": f"已联网检索到 {len(sources)} 条相关资料，正在结合资料回答"}
                )
            else:
                self._sse_event({"notice": "没有找到相关的搜索结果，将直接由本地模型回答"})

        # 注入当前日期时间：小模型容易忽略系统提示，所以同时写入系统提示和用户问题
        now = datetime.now()
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        date_str = (
            f"当前时间：{now.year}年{now.month}月{now.day}日 "
            f"{weekdays[now.weekday()]} {now.hour:02d}:{now.minute:02d}"
        )

        system_parts = []
        system_parts.append(
            date_str + "。若用户问今天/现在的日期、时间、星期几，"
            "直接复述上面的当前时间即可，不要自己计算或推算。"
        )
        # 注入所选记忆内容
        memory_content = ""
        if memory_name:
            memory_content = read_memory(self.app.cfg, memory_name)
            if memory_content.strip():
                system_parts.append(
                    "你的记忆（回答时可参考）：\n" + memory_content.strip()
                )
        prompt = self.THINK_PROMPTS.get(level)
        if prompt:
            system_parts.append(prompt)
        if sources:
            system_parts.append(
                "本轮已联网搜索，资料已附在用户的问题前面。"
                "必须优先使用这些资料回答，并在末尾用「参考：[编号]」标注；"
                "只回答一次，不要重复输出。"
            )
        elif query and want_web and not is_chitchat(query):
            system_parts.append(
                "本轮联网检索没有找到可靠资料。涉及实时信息、具体价格/参数/新闻的问题，"
                "请直接说明「没有检索到可靠资料」，不要凭记忆编造具体数据。"
            )
        if system_parts:
            messages = [{"role": "system", "content": "\n\n".join(system_parts)}] + messages

        # 把当前时间（以及联网资料）直接附加到最后一条用户消息，确保模型看到
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                messages[i] = dict(messages[i])
                question = messages[i]["content"]
                prefix = f"（{date_str}）"
                if sources:
                    prefix += "\n\n" + build_material(sources)
                messages[i]["content"] = f"{prefix}\n\n我的问题：{question}"
                break

        num_predict = self.THINK_TOKENS.get(level, 2048)
        if sources:
            # 联网回答需要引用资料，给足输出长度
            num_predict = max(num_predict, 768)
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "num_ctx": int(self.app.cfg.get("numCtx") or 8192),
                "num_predict": num_predict,
            },
            "think": level != "off",
        }
        try:
            resp = self._open_ollama(payload)
        except Exception as e:
            self._sse_event({"error": friendly_error(e)})
            return

        full_content = ""
        try:
            with resp:
                for raw in resp:
                    line = raw.strip()
                    if not line:
                        continue
                    out = line
                    if b'"error"' in line:
                        try:
                            obj = json.loads(line)
                            if obj.get("error"):
                                out = json.dumps(
                                    {"error": self._ollama_error(str(obj["error"]))},
                                    ensure_ascii=False,
                                ).encode("utf-8")
                        except Exception:
                            pass
                    else:
                        try:
                            obj = json.loads(line)
                            msg = obj.get("message") or {}
                            if isinstance(msg.get("content"), str):
                                full_content += msg["content"]
                        except Exception:
                            pass
                    self.wfile.write(b"data: " + out + b"\n\n")
                    self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        except Exception as e:
            self._sse_event({"error": friendly_error(e)})

        # 自动保留记忆：把本轮对话的关键信息写入所选记忆
        if (
            bool(self.app.cfg.get("autoMemory"))
            and memory_name
            and query
            and full_content.strip()
        ):
            threading.Thread(
                target=self._auto_memory_update,
                args=(model, memory_name, query, full_content),
                daemon=True,
            ).start()

    def _auto_memory_update(self, model: str, memory_name: str, user_text: str, assistant_text: str):
        """在后台把本轮对话的关键信息追加到记忆文件。"""
        try:
            existing = read_memory(self.app.cfg, memory_name)
            extract_prompt = (
                "请从下面的对话中提取需要长期记住的关键信息"
                "（用户偏好、事实、决定、待办、重要知识点等），"
                "用简洁的要点输出，每条一行并以「- 」开头。"
                "如果没有值得记住的信息，只回复「无」。\n\n"
                f"用户：{user_text}\n助手：{assistant_text[:4000]}"
            )
            if existing.strip():
                extract_prompt += (
                    "\n\n已有记忆如下，请不要重复已有内容，只补充新的信息：\n"
                    + existing.strip()[:6000]
                )
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": extract_prompt}],
                "stream": False,
                "options": {"num_ctx": 8192, "num_predict": 1024},
                "think": False,
            }
            try:
                data = self.app.manager._api(
                    "/api/chat", payload, method="POST", timeout=120
                )
            except Exception:
                return
            reply = ""
            try:
                reply = (data.get("message") or {}).get("content", "")
            except Exception:
                reply = ""
            reply = (reply or "").strip()
            if not reply or reply in ("无", "无。", "none", "None"):
                return
            # 清理可能的前缀
            lines = [
                ln.strip() for ln in reply.splitlines()
                if ln.strip() and not ln.strip().lower().startswith(("无", "没"))
            ]
            if not lines:
                return
            addition = "\n" + "\n".join(lines)
            new_content = (existing.rstrip() + addition).lstrip()
            write_memory(self.app.cfg, memory_name, new_content)
        except Exception:
            pass

    THINK_PROMPTS = {
        "off": "请直接给出最终答案，不要思考过程、不要分析步骤、不要重复问题，回答尽量简洁。",
        "normal": "回答要条理清晰、简洁明了。",
        "deep": "请先进行充分、严谨的推理（多角度考虑并自我校验），再给出结论；回答要条理清晰。",
    }
    THINK_TOKENS = {"off": 512, "normal": 2048, "deep": 4096}

    def _sse_event(self, obj) -> bool:
        try:
            payload = json.dumps(obj, ensure_ascii=False)
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionError, OSError):
            return False

    def _open_ollama(self, payload):
        url = self.app.manager.host + "/api/chat"

        def send(data_obj):
            req = urllib.request.Request(
                url,
                data=json.dumps(data_obj).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            return urllib.request.urlopen(req, timeout=900)

        try:
            return send(payload)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            # 旧版 Ollama 可能不支持 think 参数，去掉后重试一次
            if "think" in detail.lower() and "think" in payload:
                payload.pop("think", None)
                try:
                    return send(payload)
                except urllib.error.HTTPError as e2:
                    detail = e2.read().decode("utf-8", "replace")
            try:
                obj = json.loads(detail)
                detail = obj.get("error") or detail
            except Exception:
                pass
            raise RuntimeError(self._ollama_error(detail))

    @staticmethod
    def _ollama_error(text: str) -> str:
        low = (text or "").lower()
        if "model not found" in low or "no such model" in low:
            return "模型不存在或已被删除，请重新下载"
        if "memory" in low:
            return "内存不足，无法加载该模型，请换用更小的模型或关闭其它程序"
        if "connection refused" in low or "failed to connect" in low:
            return "无法连接 Ollama 服务，请稍后重试"
        if "context length" in low:
            return "对话超出模型上下文长度，请新建对话后再试"
        return (text or "生成失败").strip()


class LauncherServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(port: int, app: AppContext) -> LauncherServer:
    server = LauncherServer(("127.0.0.1", port), Handler)
    server.app = app
    return server

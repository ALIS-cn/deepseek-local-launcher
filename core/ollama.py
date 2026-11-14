# -*- coding: utf-8 -*-
"""Ollama 检测、下载安装、服务管理与模型拉取（国内镜像优先）"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from .catalog import CATALOG_MAP
from .config import APP_DIR, Config

RUNTIME_DIR = APP_DIR / "runtime"


def _shared_root() -> Path:
    """Ollama 运行时的统一存放目录：放在用户目录下，重装/升级应用不会丢。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "DeepSeekLauncher"


# 每个版本各自一个独立目录，互不覆盖，重装应用后仍能自动找到
RUNTIME_ROOT = _shared_root() / "runtimes"
DOWNLOAD_DIR = _shared_root() / "downloads"
SERVE_LOG = RUNTIME_ROOT / "ollama-serve.log"

MANAGED_DIRS = {
    "intel": RUNTIME_ROOT / "ollama-intel",
    "standard": RUNTIME_ROOT / "ollama-standard",
}

# 旧版本把 Ollama 解压在应用目录里，继续识别，避免升级后“找不到”
LEGACY_DIRS = {
    "intel": RUNTIME_DIR / "ollama-intel",
    "standard": RUNTIME_DIR / "ollama",
}

# Intel 优化版来自 ModelScope（https://www.modelscope.cn/models/intel/ollama）
# 标准版优先走 GitHub 国内加速镜像，失败时回退官方地址
INSTALL_SOURCES = {
    "intel": {
        "label": "Intel 优化版",
        "file": "ollama-intel-win.zip",
        "urls": [
            "https://www.modelscope.cn/models/intel/ollama/resolve/master/"
            "ollama-intel-2.3.0b20250923-win.zip",
        ],
    },
    "standard": {
        "label": "标准版",
        "file": "ollama-windows-amd64.zip",
        "urls": [
            "https://gh-proxy.com/https://github.com/ollama/ollama/releases/"
            "latest/download/ollama-windows-amd64.zip",
            "https://cdn.gh-proxy.com/https://github.com/ollama/ollama/releases/"
            "latest/download/ollama-windows-amd64.zip",
            "https://ghproxy.net/https://github.com/ollama/ollama/releases/"
            "latest/download/ollama-windows-amd64.zip",
            "https://github.com/ollama/ollama/releases/latest/download/"
            "ollama-windows-amd64.zip",
        ],
    },
}

MODELSCOPE_FILE_URL = (
    "https://www.modelscope.cn/models/{repo}/resolve/master/{file}"
)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def fmt_size(num) -> str:
    try:
        num = float(num)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def fmt_speed(num) -> str:
    return fmt_size(num) + "/s"


def friendly_error(err) -> str:
    """把常见的网络/文件异常转成中文提示。"""
    if isinstance(err, urllib.error.HTTPError):
        code = err.code
        if code == 404:
            return "下载地址不存在（404），请稍后重试"
        if code == 403:
            return "下载被服务器拒绝（403），请稍后重试"
        if code == 416:
            return "本地文件与服务器不一致，请删除后重试"
        if code >= 500:
            return f"下载服务器异常（HTTP {code}），请稍后重试"
        return f"下载失败（HTTP {code}），请稍后重试"
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", err)
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
            return "网络连接超时，请检查网络后重试"
        return f"网络连接失败（{reason}），请检查网络或代理设置"
    if isinstance(err, (socket.timeout, TimeoutError)):
        return "网络连接超时，请检查网络后重试"
    if isinstance(err, PermissionError):
        return "没有文件写入权限，请更换模型目录"
    if isinstance(err, ConnectionError):
        return "网络连接中断，请重试"
    text = str(err).strip()
    return text or "未知错误"


def translate_ollama_line(seg: str) -> str:
    """把 ollama create 等命令输出的英文状态翻译成中文。"""
    seg = re.sub(r"[\u2800-\u28FF]", "", seg).strip()
    percent = re.search(r"(\d{1,3})\s*%", seg)
    low = seg.lower()
    mapping = [
        ("gathering model components", "正在收集模型组件…"),
        ("parsing gguf", "正在解析 GGUF 模型文件…"),
        ("copying file", "正在复制模型文件"),
        ("creating new layer", "正在生成模型层…"),
        ("using existing layer", "正在使用已有模型层…"),
        ("writing manifest", "正在写入模型清单…"),
        ("verifying sha256", "正在校验文件…"),
        ("removing any unused layers", "正在清理临时文件…"),
        ("success", "完成"),
    ]
    for key, zh in mapping:
        if key in low:
            if percent and key in ("copying file", "parsing gguf"):
                return f"{zh.rstrip('…')} {percent.group(1)}%"
            return zh
    if "sha256" in low:
        return "正在处理模型文件…"
    return seg


def find_exe(folder: Path):
    if not folder or not folder.exists():
        return None
    direct = folder / "ollama.exe"
    if direct.exists():
        return direct
    for item in folder.rglob("ollama.exe"):
        return item
    return None


def download_file(url: str, dest: Path, on_progress, resume: bool = True):
    """支持断点续传的下载"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    start_size = dest.stat().st_size if (resume and dest.exists()) else 0
    headers = {"User-Agent": UA}
    if start_size:
        headers["Range"] = f"bytes={start_size}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and start_size:
            on_progress(start_size, start_size, 0)
            return
        raise
    with resp:
        if start_size and resp.status != 206:
            start_size = 0
        length = int(resp.headers.get("Content-Length") or 0)
        total = length + start_size
        mode = "ab" if start_size else "wb"
        done = start_size
        start = time.time()
        last = 0.0
        with open(dest, mode) as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last >= 0.4:
                    last = now
                    on_progress(done, total, done / max(now - start, 0.001))
        on_progress(done, total or done, done / max(time.time() - start, 0.001))


def _probe_size(url: str) -> int:
    """探测服务器是否支持分段下载，返回文件总大小。"""
    try:
        req = urllib.request.Request(
            url, headers={"Range": "bytes=0-0", "User-Agent": UA}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status == 206:
                match = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
                if match:
                    return int(match.group(1))
    except Exception:
        pass
    return 0


def download_file_fast(url: str, dest: Path, on_progress, connections: int = 8):
    """优先使用多线程分段下载；服务器不支持时回退单线程。"""
    total = _probe_size(url)
    if total < 20 * 1024 * 1024:
        download_file(url, dest, on_progress, resume=True)
        return

    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if part.exists() and part.stat().st_size == total:
        os.replace(part, dest)
        on_progress(total, total, 0)
        return
    try:
        with open(part, "wb") as f:
            f.truncate(total)
    except OSError:
        download_file(url, dest, on_progress, resume=True)
        return

    chunk = total // connections
    ranges = []
    for i in range(connections):
        start = i * chunk
        end = total - 1 if i == connections - 1 else start + chunk - 1
        ranges.append((start, end))

    done = [0] * connections
    errors = []
    lock = threading.Lock()
    started = time.time()
    last = [0.0]

    def worker(index, start, end):
        try:
            req = urllib.request.Request(
                url,
                headers={"Range": f"bytes={start}-{end}", "User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=60) as resp, open(part, "r+b") as f:
                f.seek(start)
                pos = start
                while pos <= end:
                    data = resp.read(min(262144, end - pos + 1))
                    if not data:
                        break
                    f.write(data)
                    pos += len(data)
                    with lock:
                        done[index] = pos - start
                        loaded = sum(done)
                        now = time.time()
                        if now - last[0] >= 0.4:
                            last[0] = now
                            on_progress(
                                loaded, total, loaded / max(now - started, 0.001)
                            )
            if pos <= end:
                raise RuntimeError("分段数据不完整")
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(i, s, e), daemon=True)
        for i, (s, e) in enumerate(ranges)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors or sum(done) < total:
        try:
            part.unlink(missing_ok=True)
        except Exception:
            pass
        # 分段失败时回退单线程下载
        download_file(url, dest, on_progress, resume=False)
        return

    os.replace(part, dest)
    on_progress(total, total, total / max(time.time() - started, 0.001))


def extract_zip(zip_path: Path, dest: Path, on_progress=None):
    """优先用 Python 解压；遇到 Deflate64 等不支持的方法时回退到 Windows 资源管理器解压。"""
    try:
        _extract_zip_python(zip_path, dest, on_progress)
    except (NotImplementedError, zipfile.BadZipFile) as e:
        msg = str(e).lower()
        if isinstance(e, zipfile.BadZipFile) and "compression" not in msg:
            raise
        _extract_zip_windows(zip_path, dest, on_progress)


def _extract_zip_python(zip_path: Path, dest: Path, on_progress=None):
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        total = sum(m.file_size for m in members) or 1
        done = 0
        for m in members:
            target = (dest / m.filename).resolve()
            if not str(target).startswith(str(dest_resolved)):
                continue
            zf.extract(m, dest)
            done += m.file_size
            if on_progress:
                on_progress(done, total)


def _extract_zip_windows(zip_path: Path, dest: Path, on_progress=None, timeout=1800):
    """通过 Shell.Application（资源管理器）解压，支持 Deflate64。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            total = sum(m.file_size for m in zf.infolist()) or 1
            expected = sum(1 for m in zf.infolist() if not m.is_dir())
    except Exception:
        total, expected = 0, 0

    dest.mkdir(parents=True, exist_ok=True)

    def ps_quote(value: Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$dest = {ps_quote(dest)}\n"
        f"$zipPath = {ps_quote(zip_path)}\n"
        "New-Item -ItemType Directory -Force -Path $dest | Out-Null\n"
        "$shell = New-Object -ComObject Shell.Application\n"
        "$zip = $shell.NameSpace($zipPath)\n"
        "if ($null -eq $zip) { throw '无法打开压缩包' }\n"
        "$target = $shell.NameSpace($dest)\n"
        "$target.CopyHere($zip.Items(), 532)\n"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"解压失败：{err[-300:] or '未知错误'}")

    started = time.time()
    while time.time() < started + timeout:
        files = [f for f in dest.rglob("*") if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        if on_progress:
            on_progress(size, total)
        if expected and len(files) >= expected and size >= total:
            return
        if not files and time.time() - started > 30:
            raise RuntimeError("解压失败：Windows 未复制任何文件，请检查安全软件是否拦截")
        time.sleep(0.6)
    raise RuntimeError("解压超时，请检查磁盘空间或稍后重试")


def _pid_on_port(port: int) -> int:
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        text = (out.stdout or b"").decode("utf-8", "replace")
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                return int(parts[4])
    except Exception:
        pass
    return 0


def _process_name(pid: int) -> str:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        text = (out.stdout or b"").decode("utf-8", "replace").strip()
        if text.startswith('"'):
            return text.split('","')[0].strip('"')
    except Exception:
        pass
    return ""


class OllamaManager:
    def __init__(self, cfg: Config, hub):
        self.cfg = cfg
        self.hub = hub
        self.path = None
        self.kind = None
        self.proc = None
        self.version = ""
        self.last_error = ""
        self._lock = threading.RLock()
        self._install_lock = threading.Lock()
        self._pull_lock = threading.Lock()
        self._last_chat_model = ""
        self._loaded_model = ""
        self.detect()
        self.version = self._read_version()

    # ---------- 基础信息 ----------
    @property
    def port(self) -> int:
        try:
            return int(self.cfg.get("ollamaPort") or 11500)
        except Exception:
            return 11500

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def model_dir(self) -> Path:
        return Path(self.cfg.get("modelDir"))

    @staticmethod
    def _guess_kind(path: Path) -> str:
        return "intel" if "intel" in str(path).lower() else "standard"

    def _candidates(self):
        out = []
        cfg_path = str(self.cfg.get("ollamaPath") or "").strip()
        if cfg_path:
            p = Path(cfg_path)
            if p.is_file():
                out.append((p, self._guess_kind(p)))
        for kind, folder in list(MANAGED_DIRS.items()) + list(LEGACY_DIRS.items()):
            exe = find_exe(folder)
            if exe:
                out.append((exe, kind))
        which = shutil.which("ollama")
        if which:
            out.append((Path(which), self._guess_kind(Path(which))))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for exe in (
                Path(local) / "Programs" / "Ollama" / "ollama.exe",
                Path(local) / "Ollama" / "ollama.exe",
            ):
                if exe.exists():
                    out.append((exe, "standard"))
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                exe = Path(base) / "Ollama" / "ollama.exe"
                if exe.exists():
                    out.append((exe, "standard"))
        seen = set()
        result = []
        for exe, kind in out:
            key = str(exe).lower()
            if key in seen:
                continue
            seen.add(key)
            result.append((exe, kind))
        return result

    def detect(self):
        cands = self._candidates()
        if not cands:
            self.path = None
            self.kind = None
            return None
        prefer = "intel" if bool(self.cfg.get("intelMode")) else "standard"
        chosen = next((c for c in cands if c[1] == prefer), None)
        if chosen is None:
            # 对应版本尚未安装时，只能先用已存在的运行时
            chosen = cands[0]
        old = str(self.path) if self.path else ""
        self.path, self.kind = chosen
        if str(self.path) != old:
            self.version = self._read_version()
        return self.path

    def _read_version(self) -> str:
        if not self.path:
            return ""
        try:
            with urllib.request.urlopen(self.host + "/api/version", timeout=3) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
                if data.get("version"):
                    return f"ollama {data['version']}"
        except Exception:
            pass
        try:
            env = os.environ.copy()
            env["OLLAMA_HOST"] = f"127.0.0.1:{self.port}"
            out = subprocess.run(
                [str(self.path), "--version"],
                capture_output=True,
                timeout=20,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            text = (out.stdout or b"").decode("utf-8", "replace") or (
                out.stderr or b""
            ).decode("utf-8", "replace")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for line in lines:
                if line.lower().startswith("ollama version"):
                    return line
            for line in lines:
                low = line.lower()
                if "version" in low and not low.startswith("warning"):
                    return line
            return ""
        except Exception:
            return ""

    def status(self) -> dict:
        running = self._healthy()
        if running and not self.version:
            self.version = self._read_version()
        wanted = "intel" if bool(self.cfg.get("intelMode")) else "standard"
        return {
            "installed": self.path is not None,
            "path": str(self.path) if self.path else "",
            "kind": self.kind or "",
            "isIntel": self.kind == "intel",
            "version": self.version,
            "serveRunning": running,
            "host": self.host,
            "modeMismatch": bool(self.path) and self.kind != wanted,
            "wantedKind": wanted,
            "modelDir": str(self.model_dir),
            "lastError": self.last_error,
            "intelMode": bool(self.cfg.get("intelMode")),
            "runtimeRoot": str(RUNTIME_ROOT),
            "managedDirs": {k: str(v) for k, v in MANAGED_DIRS.items()},
        }

    # ---------- 服务管理 ----------
    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.host + "/api/version", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    @staticmethod
    def _bat_env(folder: Path) -> dict:
        """Intel 版 start-ollama.bat 中可能带有必要的环境变量，读取并复用。"""
        env = {}
        bat = folder / "start-ollama.bat"
        if not bat.exists():
            return env
        try:
            text = bat.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return env
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r'^(?:set|SET)\s+"?([A-Za-z_][\w]*)=([^"]*)"?$', line)
            if not m:
                continue
            key, value = m.group(1), m.group(2).strip()
            if key.upper() in ("OLLAMA_MODELS", "OLLAMA_HOST", "PATH"):
                continue
            if key.upper().startswith(("OLLAMA_", "SYCL_", "ONEAPI_")):
                env[key] = value
        return env

    def _build_env(self) -> dict:
        env = {}
        if self.kind == "intel" and self.path:
            env.update(self._bat_env(self.path.parent))
        env.update(
            {
                "OLLAMA_MODELS": str(self.model_dir),
                "OLLAMA_HOST": f"127.0.0.1:{self.port}",
                "OLLAMA_KEEP_ALIVE": "30m",
                "OLLAMA_CONTEXT_LENGTH": str(self.cfg.get("numCtx") or 8192),
            }
        )
        return env

    def autostart(self):
        if not self.path:
            return
        try:
            if not self._healthy():
                self.start()
        except Exception as e:
            self.last_error = friendly_error(e)
            self.hub.publish(
                "ollama",
                {"status": "error", "percent": 0, "message": self.last_error},
            )

    def start(self):
        with self._lock:
            self.detect()
            if not self.path:
                raise RuntimeError("未检测到 Ollama，请先下载安装")
            if self._healthy():
                return True
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=6)
                except Exception:
                    pass
                self.proc = None

            self.model_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(self._build_env())
            SERVE_LOG.parent.mkdir(parents=True, exist_ok=True)
            self.hub.publish(
                "ollama",
                {
                    "status": "starting",
                    "percent": 0,
                    "message": "正在启动 Ollama 服务…",
                },
            )
            log = open(SERVE_LOG, "wb")
            self.proc = subprocess.Popen(
                [str(self.path), "serve"],
                cwd=str(self.path.parent),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            log.close()
            self._wait_ready()
            self.last_error = ""
            self.hub.publish(
                "ollama",
                {"status": "ready", "percent": 100, "message": "Ollama 服务已就绪"},
            )
            return True

    def _wait_ready(self, timeout=150):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc is not None and self.proc.poll() is not None:
                self.last_error = "Ollama 服务进程已退出，请查看日志"
                raise RuntimeError(self.last_error)
            if self._healthy():
                return True
            time.sleep(1)
        self.last_error = "Ollama 服务启动超时"
        raise RuntimeError(self.last_error)

    def stop(self):
        with self._lock:
            # 停止前先卸载所有已加载模型，确保显存被释放
            if bool(self.cfg.get("releaseOnStop")):
                try:
                    self.unload_all()
                except Exception:
                    pass
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=8)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            self.proc = None
            self._loaded_model = ""
            # 兼容上一次异常退出后残留的 Ollama 进程（同一端口）
            if self._healthy():
                pid = _pid_on_port(self.port)
                if pid and _process_name(pid).lower().startswith("ollama"):
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True,
                        timeout=15,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    time.sleep(1)

    def restart(self):
        self.stop()
        return self.start()

    def shutdown(self):
        self.stop()

    def ensure_running(self):
        if self._healthy():
            return
        self.start()

    # ---------- 安装 ----------
    def install(self, mode: str):
        if mode not in INSTALL_SOURCES:
            raise ValueError("未知的安装模式")
        if not self._install_lock.acquire(blocking=False):
            raise RuntimeError("已有安装任务正在进行")
        try:
            meta = INSTALL_SOURCES[mode]
            label = meta["label"]
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            zip_path = DOWNLOAD_DIR / meta["file"]
            self.hub.publish(
                "ollama",
                {
                    "status": "downloading",
                    "percent": 0,
                    "message": f"正在通过国内镜像下载 {label} …",
                },
            )
            last = [0.0]

            def on_dl(done, total, speed):
                now = time.time()
                if now - last[0] < 0.4 and done != total:
                    return
                last[0] = now
                percent = int(done * 100 / total) if total else 0
                self.hub.publish(
                    "ollama",
                    {
                        "status": "downloading",
                        "percent": percent,
                        "message": f"下载 {label} {fmt_size(done)} / {fmt_size(total)}"
                                   f" · {fmt_speed(speed)}",
                    },
                )

            errors = []
            for url in meta["urls"]:
                try:
                    download_file_fast(url, zip_path, on_dl)
                    break
                except Exception as e:
                    errors.append(f"{url.split('/')[2]}：{friendly_error(e)}")
            else:
                raise RuntimeError("下载失败：" + "；".join(errors))

            self.hub.publish(
                "ollama",
                {"status": "extracting", "percent": 0, "message": "正在解压安装包…"},
            )
            self.stop()
            RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
            dest = MANAGED_DIRS[mode]
            # 先解压到临时目录，成功后再替换旧版本，避免中途失败把能用的版本删掉
            staging = RUNTIME_ROOT / f".install-{mode}"
            shutil.rmtree(staging, ignore_errors=True)
            last_ex = [0.0]

            def on_ex(done, total):
                now = time.time()
                if now - last_ex[0] < 0.4 and done != total:
                    return
                last_ex[0] = now
                percent = int(done * 100 / total) if total else 0
                self.hub.publish(
                    "ollama",
                    {
                        "status": "extracting",
                        "percent": percent,
                        "message": f"正在解压… {percent}%",
                    },
                )

            extract_zip(zip_path, staging, on_ex)
            if not find_exe(staging):
                shutil.rmtree(staging, ignore_errors=True)
                raise RuntimeError("解压完成但未找到 ollama.exe")

            backup = RUNTIME_ROOT / f".old-{mode}"
            shutil.rmtree(backup, ignore_errors=True)
            if dest.exists():
                try:
                    dest.rename(backup)
                except Exception:
                    shutil.rmtree(dest, ignore_errors=True)
            try:
                staging.rename(dest)
            except Exception:
                shutil.copytree(staging, dest, dirs_exist_ok=True)
                shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)

            exe = find_exe(dest)
            if not exe:
                raise RuntimeError("安装失败：未找到 ollama.exe")
            zip_path.unlink(missing_ok=True)

            self.cfg.update(ollamaPath=str(exe), intelMode=(mode == "intel"))
            self.detect()
            self.hub.publish(
                "ollama",
                {
                    "status": "installed",
                    "percent": 100,
                    "message": f"{label} 安装完成，正在启动服务…",
                },
            )
            try:
                self.start()
                self.hub.publish(
                    "ollama",
                    {
                        "status": "done",
                        "percent": 100,
                        "message": f"{label} 已就绪，可以下载模型了",
                    },
                )
            except Exception as e:
                self.hub.publish(
                    "ollama",
                    {
                        "status": "done",
                        "percent": 100,
                        "message": f"安装完成，但服务启动失败：{friendly_error(e)}",
                    },
                )
        except Exception as e:
            self.last_error = friendly_error(e)
            self.hub.publish(
                "ollama", {"status": "error", "percent": 0, "message": self.last_error}
            )
        finally:
            self._install_lock.release()

    # ---------- HTTP 接口封装 ----------
    def _api(self, path, payload=None, method=None, timeout=15):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.host + path, data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}

    def list_models(self):
        if not self._healthy():
            return []
        try:
            data = self._api("/api/tags", timeout=6)
            models = []
            for m in data.get("models", []):
                details = m.get("details") or {}
                models.append(
                    {
                        "name": m.get("name", ""),
                        "size": m.get("size", 0),
                        "modified": m.get("modified_at", ""),
                        "family": details.get("family", ""),
                        "paramSize": details.get("parameter_size", ""),
                    }
                )
            models.sort(key=lambda x: x.get("modified", ""), reverse=True)
            return models
        except Exception:
            return []

    def delete_model(self, name: str):
        return self._api("/api/delete", {"model": name}, method="DELETE", timeout=30)

    def load_model(self, name: str):
        current = self._loaded_model or self._last_chat_model
        if bool(self.cfg.get("restartOnSwitch")) and current and current != name:
            # 切换模型：重启服务彻底释放显存，再加载新模型
            self.restart()
        else:
            self.ensure_running()
            # 未重启时退化为卸载已加载模型，释放显存
            if bool(self.cfg.get("releaseOnSwitch")):
                self.unload_all()
        result = self._api(
            "/api/generate",
            {"model": name, "prompt": "", "stream": False, "keep_alive": "30m"},
            timeout=600,
        )
        self._loaded_model = name
        return result

    def unload_all(self):
        """枚举当前已加载模型，逐个设置 keep_alive=0 以释放显存。"""
        if not self._healthy():
            return
        try:
            data = self._api("/api/ps", timeout=8)
        except Exception:
            return
        models = data.get("models") or []
        for m in models:
            mname = m.get("name") or m.get("model")
            if not mname:
                continue
            try:
                self._api(
                    "/api/generate",
                    {"model": mname, "prompt": "", "stream": False, "keep_alive": 0},
                    timeout=30,
                )
            except Exception:
                # 单个模型卸载失败不影响整体
                pass

    # ---------- 模型下载 ----------
    def pull(self, model: str):
        if not self._pull_lock.acquire(blocking=False):
            raise RuntimeError("已有模型正在下载，请等待完成")
        try:
            self.hub.publish(
                "model",
                {
                    "status": "preparing",
                    "percent": 0,
                    "model": model,
                    "message": f"正在准备下载 {model} …",
                },
            )
            self.ensure_running()
            entry = CATALOG_MAP.get(model)
            if entry:
                try:
                    self._pull_modelscope(model, entry)
                except Exception as e:
                    self.hub.publish(
                        "model",
                        {
                            "status": "pulling",
                            "percent": 0,
                            "model": model,
                            "message": f"国内镜像下载失败（{friendly_error(e)}），改用 Ollama 官方源…",
                        },
                    )
                    self._pull_http(model)
            else:
                self._pull_http(model)
            self.hub.publish(
                "model",
                {
                    "status": "done",
                    "percent": 100,
                    "model": model,
                    "message": f"{model} 下载完成",
                },
            )
        except Exception as e:
            self.hub.publish(
                "model",
                {
                    "status": "error",
                    "percent": 0,
                    "model": model,
                    "message": friendly_error(e),
                },
            )
            raise
        finally:
            self._pull_lock.release()

    def _check_space(self, need: int):
        try:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(str(self.model_dir))
            if usage.free < need:
                raise RuntimeError(
                    f"磁盘空间不足：需要约 {fmt_size(need)}，"
                    f"当前可用 {fmt_size(usage.free)}"
                )
        except RuntimeError:
            raise
        except Exception:
            pass

    def _pull_modelscope(self, model: str, entry: dict):
        repo = entry.get("repo")
        filename = entry.get("file")
        if not repo or not filename:
            raise RuntimeError("该模型未配置国内镜像")
        url = MODELSCOPE_FILE_URL.format(
            repo=repo, file=urllib.parse.quote(filename)
        )
        tmp_dir = self.model_dir / ".import-tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        gguf = tmp_dir / filename
        # 目录中已有完整文件时直接导入
        download_needed = not gguf.exists() or gguf.stat().st_size == 0
        if not download_needed:
            self.hub.publish(
                "model",
                {
                    "status": "pulling",
                    "percent": 100,
                    "model": model,
                    "message": "发现已下载的模型文件，直接导入…",
                },
            )
        else:
            last = [0.0]

            def on_dl(done, total, speed):
                now = time.time()
                if now - last[0] < 0.4 and done != total:
                    return
                last[0] = now
                percent = int(done * 100 / total) if total else 0
                self.hub.publish(
                    "model",
                    {
                        "status": "pulling",
                        "percent": percent,
                        "model": model,
                        "message": f"ModelScope 国内镜像下载中 {fmt_size(done)} / "
                                   f"{fmt_size(total)} · {fmt_speed(speed)}",
                    },
                )

            self.hub.publish(
                "model",
                {
                    "status": "pulling",
                    "percent": 0,
                    "model": model,
                    "message": "正在连接 ModelScope 国内镜像…",
                },
            )
            download_file(url, gguf, on_dl, resume=True)

        self._check_space(gguf.stat().st_size)
        self.hub.publish(
            "model",
            {
                "status": "importing",
                "percent": 100,
                "model": model,
                "message": "下载完成，正在导入到 Ollama（可能需要几分钟）…",
            },
        )
        modelfile = tmp_dir / "Modelfile"
        modelfile.write_text(
            f'FROM "{gguf.as_posix()}"\n', encoding="utf-8"
        )
        env = os.environ.copy()
        env["OLLAMA_HOST"] = f"127.0.0.1:{self.port}"
        proc = subprocess.Popen(
            [str(self.path), "create", model, "-f", str(modelfile)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        tail = []
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        pending = b""
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            pending += chunk
            if b"\r" not in pending and b"\n" not in pending:
                continue
            text = pending.decode("utf-8", "replace")
            pending = b""
            parts = [ANSI_RE.sub("", p).strip() for p in re.split(r"[\r\n]+", text)]
            parts = [p for p in parts if p]
            if not parts:
                continue
            tail = (tail + parts[-4:])[-8:]
            seg = parts[-1]
            self.hub.publish(
                "model",
                {
                    "status": "importing",
                    "percent": 100,
                    "model": model,
                    "message": f"导入中：{translate_ollama_line(seg)[:90]}",
                },
            )
        code = proc.wait()
        proc.stdout.close()
        if code != 0:
            detail = " / ".join(tail[-3:]) if tail else f"退出码 {code}"
            try:
                gguf.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"导入模型失败：{detail}")

        for path in (gguf, modelfile):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    def _pull_http(self, model: str):
        req = urllib.request.Request(
            self.host + "/api/pull",
            data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        last_pub = 0.0
        with urllib.request.urlopen(req, timeout=120) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("error"):
                    raise RuntimeError(obj["error"])
                status = obj.get("status", "")
                total = obj.get("total") or 0
                completed = obj.get("completed") or 0
                percent = int(completed * 100 / total) if total else 0
                now = time.time()
                if now - last_pub < 0.35 and status != "success":
                    continue
                last_pub = now
                self.hub.publish(
                    "model",
                    {
                        "status": "pulling",
                        "percent": percent,
                        "model": model,
                        "message": self._status_text(status, completed, total, percent),
                    },
                )
                if status == "success":
                    break

    @staticmethod
    def _status_text(status: str, completed, total, percent) -> str:
        if status == "pulling manifest":
            return "正在获取模型清单…"
        if status == "downloading":
            return f"官方源下载中 {fmt_size(completed)} / {fmt_size(total)} ({percent}%)"
        if status.startswith("verifying"):
            return "正在校验文件…"
        if status == "writing manifest":
            return "正在写入模型清单…"
        if status == "removing any unused layers":
            return "正在清理临时文件…"
        if status == "success":
            return "下载完成"
        return status or "下载中…"

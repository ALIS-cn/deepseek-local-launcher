# -*- coding: utf-8 -*-
"""DeepSeek 本地模型启动器 - 程序入口"""
import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core.api import AppContext, create_server  # noqa: E402
from core.config import APP_DIR, DATA_DIR, RESOURCE_DIR, Config  # noqa: E402
from core.events import EventHub  # noqa: E402
from core.ollama import OllamaManager  # noqa: E402

# 应用窗口使用独立的浏览器配置，放在安装目录下，保证单窗口
EDGE_PROFILE = APP_DIR / "runtime" / "edge-profile"

DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(DATA_DIR / "app.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)


def find_port(start: int) -> int:
    for port in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("没有可用的本地端口")


def existing_instance(port: int) -> str:
    url = f"http://127.0.0.1:{port}/api/state"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
            if data.get("version"):
                return f"http://127.0.0.1:{port}/"
    except Exception:
        pass
    # 服务繁忙时 /api/state 可能响应慢，只要端口在监听就认为实例已存在
    if port_listening(port):
        return f"http://127.0.0.1:{port}/"
    return ""


def port_listening(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.8)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


_MUTEX_HANDLE = None


def acquire_single_instance() -> bool:
    """命名互斥量：保证同一时间只有一个应用实例，避免双开出现两个窗口。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        _MUTEX_HANDLE = kernel32.CreateMutexW(
            None, False, "Local\\DeepSeekLauncher_SingleInstance"
        )
        if not _MUTEX_HANDLE:
            return True
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def find_browser() -> str:
    bases = [
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA"),
    ]
    candidates = []
    for base in bases:
        if not base:
            continue
        candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for exe in candidates:
        if exe.is_file():
            return str(exe)
    return ""


def focus_app_window() -> bool:
    """找到本项目已有的窗口并置于前台；返回是否找到。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        handles = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if "DeepSeek 本地模型启动器" in buf.value:
                handles.append(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
        if not handles:
            return False
        hwnd = handles[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def open_window(url: str):
    # 关掉已有的本应用窗口，保证始终只有一个窗口
    close_app_windows()
    _clean_profile_locks()

    exe = find_browser()
    if not exe:
        webbrowser.open(url)
        return

    EDGE_PROFILE.mkdir(parents=True, exist_ok=True)
    win_w, win_h, _ = window_geometry()
    args = [
        exe,
        f"--app={url}",
        f"--user-data-dir={EDGE_PROFILE}",
        f"--window-size={win_w},{win_h}",
        # 关掉首次运行欢迎页、默认浏览器提示和崩溃恢复气泡，避免多弹出“网页”
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-bubble",
        "--disable-features=msEdgeFirstRunExperience",
    ]
    # 注意：这里不能用 CREATE_NO_WINDOW，否则 Edge 窗口不会显示
    _launch_browser(args)
    if _wait_app_window(8.0):
        return
    # 第一次没出现窗口（通常是旧进程还没退干净），清掉重来一次
    close_app_windows()
    _clean_profile_locks()
    _launch_browser(args)
    if _wait_app_window(8.0):
        return
    if not _profile_pids():
        webbrowser.open(url)


def _launch_browser(args) -> bool:
    """启动浏览器并确认进程存活；返回 False 表示启动失败。"""
    try:
        proc = subprocess.Popen(args)
    except Exception:
        return False
    time.sleep(2.0)
    code = proc.poll()
    # 进程还在，或已被现有实例接管（退出码 0）都算成功
    return code is None or code == 0


def _has_app_window() -> bool:
    """是否已有本应用窗口（浏览器主进程存在）。"""
    if os.name != "nt":
        return False
    marker = str(EDGE_PROFILE)
    ps = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' -and "
        "$_.CommandLine -notlike '*--type=*' }} | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    return bool([line for line in _run_ps(ps).splitlines() if line.strip().isdigit()])


def _wait_app_window(timeout: float) -> bool:
    deadline = time.time() + timeout
    while True:
        if _has_app_window():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(1.0)


def _profile_pids():
    """返回使用本应用浏览器配置的所有浏览器进程 PID（含子进程）。"""
    if os.name != "nt":
        return []
    marker = str(EDGE_PROFILE)
    ps = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    return [line.strip() for line in _run_ps(ps).splitlines() if line.strip().isdigit()]


def _run_ps(script: str) -> str:
    """执行 PowerShell 并返回输出（中文系统输出可能是 GBK，按 UTF-8 宽松解码）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            creationflags=0x08000000,
        )
        return (r.stdout or b"").decode("utf-8", "ignore")
    except Exception:
        return ""


def _clean_profile_locks():
    """清理浏览器强制退出后残留的单实例锁文件。"""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "Lockfile"):
        try:
            (EDGE_PROFILE / name).unlink(missing_ok=True)
        except Exception:
            pass


def close_app_windows():
    """关闭使用本应用专用 Edge 配置的窗口进程，避免多开。"""
    if os.name != "nt":
        return
    pids = _profile_pids()
    for pid in pids:
        if int(pid) == os.getpid():
            continue
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", pid],
                capture_output=True,
                creationflags=0x08000000,
            )
        except Exception:
            pass
    # 等旧进程真正退出，避免新窗口复用旧实例导致多开
    if pids:
        time.sleep(1.0)
        if _profile_pids():
            time.sleep(1.0)


APP_TITLE = "DeepSeek 本地模型启动器"


def screen_work_area():
    """返回可用桌面区域（不含任务栏）；获取失败时给一个常见分辨率。"""
    if os.name != "nt":
        return 1920, 1040
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        rect = wintypes.RECT()
        # SPI_GETWORKAREA：排除任务栏后的可用区域
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width > 0 and height > 0:
                return width, height
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1040


def window_geometry():
    """按当前屏幕大小计算窗口尺寸：小屏自动缩小，大屏不超过 1360×940。"""
    screen_w, screen_h = screen_work_area()
    width = min(1360, max(900, int(screen_w * 0.9)))
    height = min(940, max(640, int(screen_h * 0.92)))
    min_size = (min(1000, width), min(680, height))
    return width, height, min_size


_WEBVIEW2_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
    r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
    r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
)


def webview2_version() -> str:
    """检测 WebView2 运行时版本（未安装时返回空串）。"""
    if os.name != "nt":
        return ""
    try:
        import winreg

        for path in _WEBVIEW2_KEYS:
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, path) as key:
                        value = str(winreg.QueryValueEx(key, "pv")[0] or "")
                        if value and value != "0.0.0.0":
                            return value
                except OSError:
                    continue
    except Exception:
        pass
    return ""


def ensure_webview2():
    """首次启动缺少 WebView2 时提示安装，避免只得到浏览器窗口。"""
    if os.name != "nt" or webview2_version():
        return
    try:
        import ctypes

        text = (
            "未检测到 Microsoft Edge WebView2 运行时。\n\n"
            "缺少它时应用只能用浏览器窗口打开，无法使用独立窗口。\n"
            "点「确定」打开 WebView2 下载页安装（装完重新打开应用即可），"
            "点「取消」则继续以浏览器窗口运行。"
        )
        box = ctypes.windll.user32.MessageBoxW
        choice = box(None, text, APP_TITLE, 0x00000001 | 0x00000040)  # OKCANCEL | ICONINFORMATION
        if choice == 1:
            webbrowser.open("https://go.microsoft.com/fwlink/p/?LinkId=2124703")
    except Exception:
        pass


def _set_splash(app, pct: int, text: str):
    win = getattr(app, "window", None)
    if win is None:
        return
    try:
        win.evaluate_js(
            f"window.setSplash && window.setSplash({pct}, {json.dumps(text, ensure_ascii=False)})"
        )
    except Exception:
        pass


def _wait_state(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_listening(port):
            return True
        time.sleep(0.2)
    return False


def run_desktop(app, manager, port: int, url: str) -> bool:
    """用原生窗口（WebView2）承载界面；不可用时返回 False 走 Edge 兜底。"""
    try:
        import webview
    except Exception:
        logging.warning("pywebview 不可用，回退到浏览器窗口")
        return False

    splash = (RESOURCE_DIR / "web" / "splash.html").as_uri()
    width, height, min_size = window_geometry()
    try:
        window = webview.create_window(
            APP_TITLE,
            splash,
            width=width,
            height=height,
            min_size=min_size,
            background_color="#070b16",
        )
    except Exception:
        logging.warning("创建原生窗口失败，回退到浏览器窗口", exc_info=True)
        return False

    app.window = window

    def on_closed():
        # 关闭窗口即退出应用，随后统一释放显存与进程
        app.quit_event.set()

    try:
        window.events.closed += on_closed
    except Exception:
        pass

    def bootstrap():
        _set_splash(app, 20, "正在启动本地服务…")
        _wait_state(port, 6)
        # 只等 Ollama 一小会儿，没起来也先进主界面；状态由界面实时显示
        _set_splash(app, 60, "正在启动 Ollama 服务…")
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                if manager.status().get("serveRunning"):
                    break
            except Exception:
                pass
            time.sleep(0.3)
        _set_splash(app, 92, "正在加载界面…")
        time.sleep(0.15)
        _set_splash(app, 100, "准备就绪")
        time.sleep(0.2)
        try:
            window.load_url(url)
        except Exception:
            pass

    threading.Thread(target=bootstrap, daemon=True).start()

    try:
        webview.start(gui="edgechromium" if os.name == "nt" else None)
        return True
    except Exception:
        logging.warning("原生窗口启动失败，回退到浏览器窗口", exc_info=True)
        app.window = None
        return False


def main():
    parser = argparse.ArgumentParser(description="DeepSeek 本地模型启动器")
    parser.add_argument("--no-window", action="store_true", help="不自动打开应用窗口")
    parser.add_argument("--port", type=int, default=0, help="指定面板端口")
    args = parser.parse_args()

    cfg = Config()
    preferred = args.port or int(cfg.get("panelPort") or 18765)

    running = existing_instance(preferred)
    if running:
        logging.info("检测到已运行的实例")
        if not args.no_window and not focus_app_window():
            open_window(running)
        return
    if not acquire_single_instance():
        logging.info("另一个实例正在启动，本次启动直接退出")
        return

    hub = EventHub()
    manager = OllamaManager(cfg, hub)
    app = AppContext(cfg, hub, manager)

    port = find_port(preferred)
    if port != int(cfg.get("panelPort") or 18765):
        cfg.update(panelPort=port)

    server = create_server(port, app)

    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=manager.autostart, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    logging.info("服务已启动: %s", url)

    native = False
    if not args.no_window:
        ensure_webview2()
        native = run_desktop(app, manager, port, url)
        if not native:
            open_window(url)

    try:
        while not app.quit_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        logging.info("应用退出，正在释放显存并停止服务")
        app.window = None
        manager.shutdown()
        server.shutdown()
        logging.info("已退出")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.error("程序异常退出\n%s", traceback.format_exc())
        raise

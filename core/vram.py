# -*- coding: utf-8 -*-
"""显存占用：读取每个进程的专用显存用量，并按进程树归属到本应用"""
import json
import os
import subprocess
import threading
import time

from .config import APP_DIR, LEGACY_ROOT
from .hardware import gpu_summary

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()

_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$parents = @{}
$paths = @{}
try {
  Get-CimInstance Win32_Process | ForEach-Object {
    $procId = [int]$_.ProcessId
    $parents[$procId] = [int]$_.ParentProcessId
    $paths[$procId] = [string]$_.ExecutablePath
  }
} catch {}
$proc = @{}
try {
  $samples = (Get-Counter '\GPU Process Memory(*)\Dedicated Usage' -ErrorAction Stop).CounterSamples
} catch { $samples = @() }
foreach ($s in $samples) {
  if ($s.InstanceName -match '^pid_(\d+)') {
    $procId = [int]$Matches[1]
    if (-not $proc.ContainsKey($procId)) { $proc[$procId] = 0.0 }
    $proc[$procId] = $proc[$procId] + [double]$s.CookedValue
  }
}
$rows = @()
foreach ($key in $proc.Keys) {
  $bytes = [int64]$proc[$key]
  if ($bytes -le 0) { continue }
  $p = Get-Process -Id $key -ErrorAction SilentlyContinue
  $name = ''
  if ($p) { $name = $p.ProcessName }
  $ppid = 0
  if ($parents.ContainsKey([int]$key)) { $ppid = $parents[[int]$key] }
  $exe = ''
  if ($paths.ContainsKey([int]$key)) { $exe = [string]$paths[[int]$key] }
  $rows += [pscustomobject]@{ pid = [int]$key; name = $name; bytes = $bytes; ppid = [int]$ppid; exe = $exe }
}
$adapters = @()
try {
  $adapters = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -ErrorAction Stop).CounterSamples |
    ForEach-Object { [pscustomobject]@{ instance = $_.InstanceName; bytes = [int64]$_.CookedValue } }
} catch {}
[pscustomobject]@{ processes = $rows; adapters = $adapters } | ConvertTo-Json -Depth 5 -Compress
"""


def _query() -> dict:
    try:
        out = subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
            capture_output=True,
            timeout=25,
            creationflags=CREATE_NO_WINDOW,
        )
        raw = (out.stdout or b"").decode("utf-8", "ignore").strip()
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    processes = data.get("processes") or []
    if isinstance(processes, dict):
        processes = [processes]
    adapters = data.get("adapters") or []
    if isinstance(adapters, dict):
        adapters = [adapters]

    # 把本应用派生出来的进程（Ollama 服务、llama-server、WebView2 子进程）都算作“本应用”
    my_pid = os.getpid()
    children = {}
    for item in processes:
        try:
            children.setdefault(int(item.get("ppid") or 0), []).append(int(item["pid"]))
        except Exception:
            continue
    ours = {my_pid}
    stack = [my_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in ours:
                ours.add(child)
                stack.append(child)

    # 本应用占用 = 自身派生的进程（WebView2 等）+ 本应用运行时目录下的 Ollama
    roots = [
        str(APP_DIR / "runtimes").lower(),
        str(APP_DIR / "runtime").lower(),
        str(LEGACY_ROOT / "runtimes").lower(),
    ]
    for item in processes:
        exe = str(item.get("exe") or "").lower()
        by_tree = int(item.get("pid") or 0) in ours
        by_path = bool(exe) and any(exe.startswith(root) for root in roots)
        item["ours"] = by_tree or by_path
    processes.sort(key=lambda p: p.get("bytes") or 0, reverse=True)

    used = sum(int(a.get("bytes") or 0) for a in adapters)
    info = gpu_summary()
    total = sum(int(g.get("vram") or 0) for g in info.get("gpus") or [])
    app_used = sum(int(p.get("bytes") or 0) for p in processes if p.get("ours"))
    return {
        "total": total,
        "used": used,
        "appUsed": app_used,
        "gpus": info.get("gpus") or [],
        "processes": processes[:15],
        "appPid": my_pid,
    }


def usage(cache_seconds: float = 1.5) -> dict:
    """读取显存占用（带短缓存，避免频繁调用 PowerShell）。"""
    now = time.time()
    with _lock:
        if _cache["data"] and now - _cache["ts"] < cache_seconds:
            return _cache["data"]
    data = _query()
    with _lock:
        _cache["ts"] = now
        _cache["data"] = data
    return data

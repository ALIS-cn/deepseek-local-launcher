# -*- coding: utf-8 -*-
"""硬件检测：读取显卡型号与显存，用于推荐合适的模型"""
import os
import re
import subprocess

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 显卡驱动注册表键（可拿到比 AdapterRAM 更准的显存大小）
_GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def _nvidia():
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        text = (out.stdout or b"").decode("utf-8", "ignore")
    except Exception:
        return []
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            size = int(float(parts[1]))
        except Exception:
            continue
        if size > 0:
            gpus.append({"name": parts[0], "vram": size * 1024**2, "source": "nvidia-smi"})
    return gpus


def _registry():
    if os.name != "nt":
        return []
    try:
        import winreg
    except Exception:
        return []
    gpus = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS)
    except OSError:
        return []
    index = 0
    while True:
        try:
            sub = winreg.OpenKey(key, winreg.EnumKey(key, index))
        except OSError:
            break
        index += 1
        try:
            desc = str(winreg.QueryValueEx(sub, "DriverDesc")[0])
            size = 0
            for name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                try:
                    raw = winreg.QueryValueEx(sub, name)[0]
                    if isinstance(raw, bytes):
                        size = int.from_bytes(raw, "little")
                    else:
                        size = int(raw)
                    break
                except OSError:
                    continue
            if desc and size > 0:
                gpus.append({"name": desc, "vram": size, "source": "registry"})
        except OSError:
            continue
        finally:
            try:
                winreg.CloseKey(sub)
            except Exception:
                pass
    try:
        winreg.CloseKey(key)
    except Exception:
        pass
    return gpus


def _wmi_names():
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_VideoController | "
        "ForEach-Object { $_.Name + '|' + $_.AdapterRAM }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=12,
            creationflags=CREATE_NO_WINDOW,
        )
        text = (out.stdout or b"").decode("utf-8", "ignore")
    except Exception:
        return []
    gpus = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        name, _, raw = line.partition("|")
        name = name.strip()
        try:
            size = int(raw.strip() or 0)
        except Exception:
            size = 0
        if name:
            gpus.append({"name": name, "vram": size, "source": "wmi"})
    return gpus


def _merge(groups):
    merged = {}
    for group in groups:
        for gpu in group:
            name = gpu["name"].strip()
            key = re.sub(r"\s+", " ", name).lower()
            current = merged.get(key)
            # 同名显卡保留更大的显存读数（WMI 对 4GB 以上常不准）
            if not current or gpu["vram"] > current["vram"]:
                merged[key] = {"name": name, "vram": gpu["vram"], "source": gpu["source"]}
    return sorted(merged.values(), key=lambda g: g["vram"], reverse=True)


def gpu_summary() -> dict:
    gpus = _merge([_nvidia(), _registry(), _wmi_names()])
    # 过滤虚拟显卡/远程桌面适配器
    gpus = [
        g for g in gpus
        if not re.search(r"virtual|remote|basic display|microsoft|idd", g["name"], re.I)
    ]
    # 读不到显存容量的设备（多为虚拟显示器）只在没有其它显卡时保留
    with_vram = [g for g in gpus if g["vram"] > 0]
    if with_vram:
        gpus = with_vram
    max_vram = max((g["vram"] for g in gpus), default=0)
    return {
        "gpus": [
            {
                "name": g["name"],
                "vram": g["vram"],
                "vramGB": round(g["vram"] / 1024**3, 1) if g["vram"] else 0,
            }
            for g in gpus
        ],
        "maxVramGB": round(max_vram / 1024**3, 1) if max_vram else 0,
    }

# -*- coding: utf-8 -*-
"""在线更新：查询 GitHub 最新版本，并通过国内加速节点下载安装包"""
import json
import os
import re
import urllib.error
import urllib.request

from .ollama import download_file_fast, fmt_size, fmt_speed

OWNER = "ALIS-cn"
REPO = "deepseek-local-launcher"
UA = "DeepSeekLauncher-Updater"

# 国内加速节点（依次尝试），最后回退 GitHub 官方
MIRRORS = (
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "",
)

API_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{OWNER}/{REPO}/releases/latest"


def parse_version(text: str):
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def _get_json(url: str, timeout: int):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get_final_url(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl()


def latest_release(timeout: int = 8) -> dict:
    """查询最新版本；GitHub API 不通时用加速节点解析 releases/latest 跳转。"""
    try:
        data = _get_json(API_URL, timeout)
        tag = str(data.get("tag_name") or "")
        return {
            "tag": tag,
            "version": tag.lstrip("vV"),
            "body": str(data.get("body") or ""),
            "url": str(data.get("html_url") or ""),
            "source": "api",
        }
    except Exception:
        pass
    for prefix in MIRRORS:
        url = prefix + RELEASE_PAGE if prefix else RELEASE_PAGE
        try:
            final = _get_final_url(url, timeout)
        except Exception:
            continue
        match = re.search(r"/tag/(v?[\d.]+)", final)
        if match:
            tag = match.group(1)
            return {
                "tag": tag,
                "version": tag.lstrip("vV"),
                "body": "",
                "url": f"https://github.com/{OWNER}/{REPO}/releases/tag/{tag}",
                "source": "mirror",
            }
    raise RuntimeError("无法连接 GitHub 检查更新，请稍后重试")


def installer_name(version: str) -> str:
    return f"DeepSeekLauncher-Setup-{version}.exe"


def download_installer(version: str, dest, on_progress) -> str:
    """通过国内节点下载安装包，返回实际使用的节点（空串表示官方）。"""
    name = installer_name(version)
    path = f"https://github.com/{OWNER}/{REPO}/releases/download/v{version}/{name}"
    last_error = None
    for prefix in MIRRORS:
        url = prefix + path if prefix else path
        try:
            download_file_fast(url, dest, on_progress)
            return prefix or "official"
        except Exception as e:  # noqa: BLE001
            last_error = e
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except Exception:
                pass
    raise RuntimeError(f"下载安装包失败：{last_error}")


def progress_message(done, total, speed) -> str:
    return f"正在下载更新 {fmt_size(done)} / {fmt_size(total)} · {fmt_speed(speed)}"

# -*- coding: utf-8 -*-
"""应用配置读写（数据与运行时默认放在应用目录下，可随应用整体移动）"""
import json
import os
import shutil
import sys
import threading
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包成 exe 后：程序在安装目录，静态资源在打包解压目录
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent.parent
    RESOURCE_DIR = APP_DIR

DATA_DIR = APP_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
VERSION_FILE = RESOURCE_DIR / "VERSION"

# 旧版本（1.5.x）把数据放在用户目录，首次运行新版时搬回应用目录
def _legacy_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "DeepSeekLauncher"


LEGACY_ROOT = _legacy_root()
LEGACY_DATA_DIR = LEGACY_ROOT / "data"
LEGACY_MEMORY_DIR = LEGACY_DATA_DIR / "memories"


def migrate_legacy_data():
    """把旧位置（用户目录）的配置、记忆与 Ollama 运行时搬到应用目录。"""
    legacy_config = LEGACY_DATA_DIR / "config.json"
    if not CONFIG_FILE.exists() and legacy_config.is_file():
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_config, CONFIG_FILE)
            memories = LEGACY_DATA_DIR / "memories"
            if memories.is_dir() and not (DATA_DIR / "memories").exists():
                shutil.copytree(memories, DATA_DIR / "memories")
        except Exception:
            pass
    # Ollama 运行时整体搬回应用目录
    src = LEGACY_ROOT / "runtimes"
    dst = APP_DIR / "runtimes"
    if src.is_dir() and not dst.exists():
        try:
            shutil.move(str(src), str(dst))
        except Exception:
            try:
                shutil.copytree(src, dst, dirs_exist_ok=True)
            except Exception:
                pass
    _remap_legacy_paths()


def _remap_legacy_paths():
    """配置里还指向用户目录的路径，改到应用目录下。"""
    if not CONFIG_FILE.exists():
        return
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    legacy = str(LEGACY_ROOT).lower()
    changed = False
    for key in ("ollamaPath", "memoryDir", "modelDir"):
        value = str(data.get(key) or "")
        if value.lower().startswith(legacy):
            data[key] = str(APP_DIR) + value[len(str(LEGACY_ROOT)):]
            changed = True
    if changed:
        try:
            CONFIG_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass


migrate_legacy_data()

DEFAULTS = {
    "modelDir": str(APP_DIR / "models"),
    "intelMode": False,
    "ollamaPath": "",
    "panelPort": 18765,
    "ollamaPort": 11500,
    "numCtx": 8192,
    # 显存释放
    "releaseOnSwitch": True,   # 切换模型时先卸载旧模型，释放显存
    "releaseOnStop": True,     # 停止服务时先卸载所有模型，释放显存
    "restartOnSwitch": True,   # 切换模型时自动重启 Ollama，彻底释放显存
    # 记忆
    "memoryDir": str(DATA_DIR / "memories"),
    "autoMemory": False,       # 对话结束后自动把关键信息写入所选记忆
    # 更新与引导
    "checkUpdate": False,      # 启动时检查更新（默认关闭）
    "firstRunDone": False,     # 是否已完成首次启动引导
}


def read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "1.0.0"


class Config:
    def __init__(self):
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception:
                pass
        # 记忆目录仍是旧的用户目录路径时，改回应用目录
        if self.data.get("memoryDir") == str(LEGACY_MEMORY_DIR):
            self.data["memoryDir"] = str(DATA_DIR / "memories")
            self.save()

    def save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换，避免中途崩溃/断电把配置写坏
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, CONFIG_FILE)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, **kwargs):
        with self._lock:
            self.data.update(kwargs)
            self.save()
        return self.data

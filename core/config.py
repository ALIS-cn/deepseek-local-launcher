# -*- coding: utf-8 -*-
"""应用配置读写（数据放在用户目录，重装/覆盖安装不会丢）"""
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


def _shared_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "DeepSeekLauncher" / "data"


DATA_DIR = _shared_data_dir()
CONFIG_FILE = DATA_DIR / "config.json"
VERSION_FILE = RESOURCE_DIR / "VERSION"

# 旧版本把配置和记忆放在安装目录的 data/ 下
LEGACY_DATA_DIR = APP_DIR / "data"
LEGACY_MEMORY_DIR = LEGACY_DATA_DIR / "memories"


def migrate_legacy_data():
    """首次运行新版时，把安装目录里的配置与记忆搬到用户目录。"""
    if CONFIG_FILE.exists() or not LEGACY_DATA_DIR.exists():
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cfg = LEGACY_DATA_DIR / "config.json"
        if cfg.is_file():
            shutil.copy2(cfg, CONFIG_FILE)
        memories = LEGACY_DATA_DIR / "memories"
        if memories.is_dir():
            target = DATA_DIR / "memories"
            if not target.exists():
                shutil.copytree(memories, target)
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
        # 记忆目录仍是旧的安装目录路径时，改到用户目录
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

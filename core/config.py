# -*- coding: utf-8 -*-
"""应用配置读写"""
import json
import os
import sys
import threading
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包成 exe 后：数据目录跟随程序所在（安装）目录，静态资源在打包解压目录
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent.parent
    RESOURCE_DIR = APP_DIR

DATA_DIR = APP_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
VERSION_FILE = RESOURCE_DIR / "VERSION"

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
    "memoryDir": str(APP_DIR / "data" / "memories"),
    "autoMemory": False,       # 对话结束后自动把关键信息写入所选记忆
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

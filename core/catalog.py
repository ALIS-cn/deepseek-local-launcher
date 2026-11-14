# -*- coding: utf-8 -*-
"""DeepSeek 模型目录（ModelScope 国内镜像 + Ollama 官方）"""

CATALOG = [
    {
        "name": "deepseek-r1:1.5b",
        "size": "1.1 GB",
        "vram": "2 GB",
        "ram": "4 GB",
        "note": "极致轻量，老电脑也能流畅运行",
        "repo": "unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF",
        "file": "DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf",
    },
    {
        "name": "deepseek-r1:7b",
        "size": "4.7 GB",
        "vram": "6 GB",
        "ram": "8 GB",
        "note": "最推荐的入门尺寸，综合表现均衡",
        "repo": "unsloth/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        "file": "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf",
    },
    {
        "name": "deepseek-r1:8b",
        "size": "4.9 GB",
        "vram": "6 GB",
        "ram": "8 GB",
        "note": "Llama 蒸馏版，推理与写作能力更强",
        "repo": "unsloth/DeepSeek-R1-Distill-Llama-8B-GGUF",
        "file": "DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf",
    },
    {
        "name": "deepseek-r1:14b",
        "size": "9.0 GB",
        "vram": "12 GB",
        "ram": "16 GB",
        "note": "进阶尺寸，复杂问题表现更好",
        "repo": "unsloth/DeepSeek-R1-Distill-Qwen-14B-GGUF",
        "file": "DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf",
    },
    {
        "name": "deepseek-r1:32b",
        "size": "19.9 GB",
        "vram": "24 GB",
        "ram": "32 GB",
        "note": "高性能设备，接近满血体验",
        "repo": "unsloth/DeepSeek-R1-Distill-Qwen-32B-GGUF",
        "file": "DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf",
    },
    {
        "name": "deepseek-r1:70b",
        "size": "42.5 GB",
        "vram": "48 GB",
        "ram": "64 GB",
        "note": "旗舰尺寸，需要大内存或高端显卡",
        "repo": "unsloth/DeepSeek-R1-Distill-Llama-70B-GGUF",
        "file": "DeepSeek-R1-Distill-Llama-70B-Q4_K_M.gguf",
    },
]

CATALOG_MAP = {item["name"]: item for item in CATALOG}

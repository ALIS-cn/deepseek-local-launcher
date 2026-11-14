# DeepSeek 本地模型启动器

一个面向 Windows 的桌面小工具：把 **Ollama + DeepSeek-R1** 的下载、安装、模型管理和聊天界面打包成一个应用，开箱即用，对话完全在本地完成。

- 原生独立窗口（WebView2），不依赖浏览器
- 一键从国内镜像（ModelScope / GitHub 加速）下载 Ollama 与模型，无需科学上网
- 本地离线对话，支持思考强度切换、记忆文件、联网搜索
- 显存管理：切换模型自动重启 / 卸载，退出时释放显存，不留残留进程

## 功能

| 模块 | 说明 |
| --- | --- |
| 模型管理 | 已安装模型列表、体积占用统计、删除；下载页展示每个模型的**建议显存**与建议内存 |
| 模型下载 | ModelScope 国内镜像优先，断点续传，失败自动回退 Ollama 官方源；支持自定义模型名 |
| 对话 | 流式输出、思考过程与最终回答分区显示、tokens/s 统计、快捷提问 |
| 联网搜索 | 开关关闭时完全不联网；开启后每轮先检索必应，自动压缩问句、去广告、按相关度排序并抓取网页正文摘录 |
| 记忆 | 选择记忆文件注入对话；可开启“自动保留记忆”，对话结束后自动提炼要点追加到记忆文件 |
| 显存 | 切换模型可自动重启 Ollama 或卸载旧模型；停止服务 / 退出应用时卸载所有模型释放显存 |
| 显卡与更新 | 自动识别显卡型号与显存并推荐模型；可开启启动时检查更新（默认关闭，安装包走国内加速节点） |
| 启动体验 | 先显示窗口与启动进度（启动服务 → 启动 Ollama → 加载模型 → 就绪），窗口大小随屏幕自适应 |

## 系统要求

- Windows 10 / 11（x64）
- 4 GB 内存起（1.5B 模型），8 GB 以上推荐
- WebView2 Runtime（Win11 自带；Win10 若缺失请安装 Microsoft Edge WebView2 Runtime）
- 磁盘空间按模型大小预留，模型默认存放在应用目录下的 `models/`

## 安装使用

1. 到 [Releases](https://github.com/ALIS-cn/deepseek-local-launcher/releases) 下载 `DeepSeekLauncher-Setup-x.y.z.exe` 并安装（默认装到当前用户目录，无需管理员权限）。
2. 首次启动后，在「设置」页安装 Ollama（Intel 优化版 / 标准版）。
3. 到「下载模型」选择合适的尺寸（按卡片上的建议显存挑选），等待下载与导入完成。
4. 回到「对话」开始使用。

数据位置：

- 配置、日志与记忆：`%LOCALAPPDATA%\DeepSeekLauncher\data`（首次运行会自动从旧安装目录迁移）
- 模型：默认安装目录下 `models/`（可在设置中修改）
- Ollama 运行时：`%LOCALAPPDATA%\DeepSeekLauncher\runtimes\ollama-intel`、`ollama-standard`
- 服务端口：面板 `18765`，Ollama `11500`（均只监听 127.0.0.1）

## 从源码运行

```powershell
# 依赖
pip install -r requirements.txt

# 启动（会自动打开原生窗口）
python app.py
```

## 打包

需要 PyInstaller 与 Inno Setup 6：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

产物：

- 免安装版：`dist\DeepSeekLauncher\DeepSeekLauncher.exe`
- 安装包：`dist\installer\DeepSeekLauncher-Setup-<版本>.exe`

## 目录结构

```
app.py                 程序入口、原生窗口、单实例、退出清理
core/
  api.py               本地 HTTP 服务（静态页 + API + SSE 事件流）
  ollama.py            Ollama 检测/下载安装/服务管理/模型拉取
  search.py            联网搜索（必应网页解析、相关性排序、正文摘录）
  config.py            配置读写（安装目录 data/config.json）
  catalog.py           内置模型目录（含建议显存/内存）
  events.py            SSE 事件总线
web/                   前端页面（原生窗口内加载）
packaging/             PyInstaller + Inno Setup 打包脚本
VERSION                当前版本号
CHANGELOG.md           更新日志
```

## 隐私说明

对话内容不会上传到任何服务器，模型推理全部在本机完成。只有开启「联网搜索」时，才会把当前提问的检索关键词发送到必应并取回公开网页内容。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

# 0018：用 TypeScript、React 和 Tauri 构建现代玩家工作台

- 状态：已接受
- 日期：2026-08-26

## 背景

ADR 0017 的 Tkinter 界面已经证明玩家可以通过一个窗口调用完整自动流程，也稳定了阶段进度和
凭据边界。但实际窗口的布局、视觉层次和可扩展交互不足以承载普通玩家工作台。用户明确选择
TypeScript 路线，希望先快速得到大致可用的现代界面，再继续改进兼容性和译文质量。

更换界面技术不能让 Web 前端获得任意文件、Shell 或游戏写入能力，也不能把 Python 中已经验证
的提取、Provider、质量检查、渲染和发布逻辑复制到前端。API key 仍不能出现在命令行、环境、
SQLite 或日志中。第一步还必须保持可逆，不把正式安装器或 Python 打包混入界面里程碑。

## 决定

现代工作台使用 React、TypeScript、Vite 和 Tauri 2。React 负责三步配置、进度、结果和错误展示；
Tauri 只开放原生文件夹选择和一个类型化的 `start_translation` 命令。前端不获得通用文件系统、
Shell、HTTP Provider 或 SDK 执行能力。

原 Tkinter 界面的应用逻辑抽到 `galtrans.player`，Tkinter 与新桌面桥都调用这一个服务。
`galtrans.desktop_bridge` 接收关闭式 v1 JSON 请求，并把现有类型化进度转换为关闭式 v1 JSONL
事件。请求最大 16 KiB；失败事件会隐藏实际 API key；桥本身不新增游戏文件处理逻辑。

Tauri Rust 后端固定启动仓库 `.venv\Scripts\python.exe -m galtrans.desktop_bridge`，固定仓库工作
目录和 `PYTHONPATH`，并显式移除 `GALTRANS_API_KEY`。请求只通过子进程标准输入发送，事件只从
标准输出读取；单行、标准错误和文本字段都有上限。Rust 重新校验事件版本、阶段、批次计数、
质量结果、输出字段和终态，并用进程内原子状态拒绝同时启动第二个任务。异常事件或进程状态不
一致时安全停止。

API key 由 React 表单传给 Tauri 命令，任务开始后立即从输入状态清空。它不进入命令行、环境
变量、Tauri 日志或持久化状态。当前不能安全证明同步 Provider 请求状态时仍不提供强制取消。

Tkinter 入口继续保留为轻量回退。ADR 0017 的共享应用服务、进度阶段、后台执行和凭据原则继续
有效；本决定只替换首选界面技术和界面到 Python 的进程边界。

## 推迟

本增量不实现正式 Windows 安装器、代码签名、自动更新、Python sidecar 打包、自动下载或发现
Ren'Py SDK、普通成品 `.rpa/.rpyc`、真实商业 Provider 实机调用、费用统计、自动启动游戏、完整
游戏副本组装、人工逐条审阅、自动语义审校或强制取消。

## 结果

玩家现在可以在现代原生 Windows 窗口中配置并观察同一个 source-only 自动流程；表单校验、目录
选择、进度、质量警告和失败信息都有明确的视觉层次。代价是开发版新增锁定的 Node 和 Rust 构建
依赖，而且当前可执行文件仍依赖仓库路径与 `.venv`，不能作为独立安装包分发。下一里程碑应先把
固定 Python 后端与必要资源打包为 sidecar，再讨论安装、签名和更新。

# 0020：把固定 Python 后端打包为 Tauri Windows sidecar

- 状态：已接受
- 日期：2026-08-27

## 背景

ADR 0018 的现代工作台已经用关闭式 JSON/JSONL 进程桥复用 Python 自动流程，但 Rust 后端固定
启动仓库 `.venv\Scripts\python.exe -m galtrans.desktop_bridge`，并设置仓库工作目录与
`PYTHONPATH`。这适合界面原型，却使编译后的工作台仍依赖开发仓库和本机 Python 环境，不能形成
一个可独立试用的 Windows 应用目录。

本增量只消除这项运行时依赖。它不能扩大前端权限，也不能借打包之名加入安装器、签名、自动
更新、SDK 下载、成品游戏导入或真实 Provider 测试。

## 决定

使用固定版本的 PyInstaller 作为仅构建期依赖，把 `galtrans.desktop_bridge`、可达的 GalTrans
模块、CPython 运行时及其所需标准库冻结为一个控制台 Windows 可执行文件。构建脚本只接受当前
Windows MSVC 主机的 Rust 目标三元组，将通过冒烟测试的文件复制为
`desktop/src-tauri/binaries/galtrans-backend-<target-triple>.exe`；该目录中的生成物不提交 Git。

冒烟测试向冻结进程的标准输入发送一个带假凭据、但项目路径不存在的合法 v1 请求。它要求进程
只返回可解析的 v1 JSONL 进度事件和一个失败终态、以退出码 1 结束，并证明假凭据未出现在输出。
这样可以在不调用 SDK、Provider 或写入游戏的情况下检查解释器、GalTrans 模块、SQLite/SSL 等
所需运行资源和桥协议已经进入 sidecar。

Tauri 在 `bundle.externalBin` 中登记这个固定名称。构建时会去掉目标三元组后缀并把
`galtrans-backend.exe` 复制到应用资源目录。Rust 只从该目录构造无参数命令，不再定位仓库、
`.venv`、模块入口或 `PYTHONPATH`；子进程工作目录也固定为应用资源目录。它继续通过标准输入发送
请求、从标准输出读取受限 JSONL，并显式移除 `GALTRANS_API_KEY`、`PYTHONHOME` 和 `PYTHONPATH`。
Windows 子进程使用无控制台窗口标志。

继续直接使用 Rust `std::process::Command`，不加入 Tauri Shell 插件。前端 capability 仍只有
`core:default` 与目录选择；唯一业务入口仍是类型化的 `start_translation`，因此 Web 前端没有
获得 sidecar 名称、任意命令、参数、环境变量或文件系统访问权。

Tauri bundler 保持关闭。`tauri build` 只生成 release 应用和同目录 sidecar，不生成 NSIS/MSI、
签名材料、更新元数据或发布包。CI 会安装固定构建依赖、重建并冒烟测试 sidecar，再运行 Python、
TypeScript、Rust 测试和无安装器 Tauri build。

## 推迟

本增量不实现 Windows 安装器、代码签名、自动更新、SDK 下载或发现、成品 `.rpa/.rpyc` 导入、
兼容性报告 UI、自动启动游戏、完整游戏副本组装、真实商业 Provider 调用或费用验证。Tkinter
回退入口和开发脚本仍使用仓库 `.venv`；只有现代 Tauri 工作台的运行时切换到 sidecar。

## 结果

现代工作台的运行目录只需保留 Tauri 主程序和同目录 `galtrans-backend.exe`；启动翻译时不再
依赖仓库 `.venv` 或源码。API key、source-only Ren'Py、只读输入、确定性校验、全新输出和单任务
边界保持不变。

代价是从源码构建新增一个固定的 PyInstaller 依赖，单文件 sidecar 启动时会在系统临时目录展开
内部运行资源，首次启动可能更慢，未签名可执行文件也可能触发安全软件提示。当前只验证 Windows
MSVC 主机构建；跨平台或交叉编译必须在有独立需求和测试矩阵后另行设计。

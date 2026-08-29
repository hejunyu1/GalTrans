# 架构概览

本文档描述 GalTrans V0.4.4 已实现的结构。项目当前停止主动开发，因此它不是未来路线图或兼容性
承诺。

## 设计目标

GalTrans 把不可信或可能产生副作用的步骤限制在明确边界内：

- 输入游戏和源码项目保持只读；
- 模型只返回结构化翻译建议，不直接操作文件；
- 确定性代码验证来源、格式和 Ren'Py 标记；
- SDK 只处理系统临时目录中的副本；
- 结果只发布到调用方指定的全新目录。

## 代码结构

```text
src/galtrans/
├── scanner.py              通用只读目录扫描与编码识别
├── ir.py                   统一文本段表示
├── adapters/renpy/         Ren'Py 提取、模板、导出与验证
├── providers/              模型服务适配器
├── translation.py          任务、批次、提案和检查点结构
├── pipeline.py             可恢复的批次执行
├── storage.py              SQLite 状态与结果存储
├── qa.py                   确定性质量规则
├── automated.py            source-only 自动流程编排
├── player.py               图形界面的应用服务边界
└── desktop_bridge.py       Tauri sidecar JSON/JSONL 协议

desktop/
├── src/                    React + TypeScript 前端
└── src-tauri/              Rust/Tauri 原生窗口与进程桥
```

## 数据流

```text
Ren'Py 源码项目（只读）
        │
        ├─ GalTrans 保守提取
        └─ 临时项目中的 Ren'Py 官方模板
                    │
                    ▼
             已交叉检查的文本段
                    │
                    ▼
        OpenAI 兼容 Provider 的结构化提案
                    │
                    ▼
        schema、来源、标记与质量规则校验
                    │
                    ▼
        临时 SDK/项目副本中的 lint 与 compile
                    │
                    ▼
          全新 game/tl/<language> 输出
```

翻译任务和请求回执保存在输入项目之外的 SQLite 数据库中。稳定任务 ID、批次 ID 和请求 ID 用于
恢复中断任务，并避免已确认成功的请求被重复发送。无法确定网络请求结果时，流程停止而不是猜测。

## Ren'Py 适配器

适配器支持常见的单语句角色台词、旁白和菜单选项。每个文本段绑定源路径、行号、编码、文件
SHA-256、有限上下文和受保护标记。复杂翻译块不会被推断为简单文本。

导出器根据 Ren'Py 官方模板重建翻译片段，验证提案身份及变量/文本标签，然后写入独立
`game/tl/<language>` 目录。验证器在完整可写的 SDK 和 source-only 项目临时副本上运行 lint、
compile，并可选择执行基础窗口启动检查。

`adapters/renpy/packaged_import.py` 包含一个未公开到 CLI/界面的内部导入边界。它只接受普通
`RPA-3.0` 中的单段 `.rpy/.rpym`，使用受限反序列化和资源上限，并生成带哈希的只读来源清单。
它不会处理编译脚本、资产、未知格式或受保护内容。

## Provider 与凭据

当前网络适配器使用 OpenAI 兼容 Chat Completions JSON 接口。发送内容仅包括筛选后的文本段、
有限上下文、来源摘要和受保护标记；项目绝对路径不会进入请求。

API key 由调用进程从指定环境变量读取，随后从传给 SDK 的环境中移除。它不保存在命令行、任务
数据库或输出报告中。由于文本会离开本机，调用方仍需自行评估 Provider 的隐私和授权条件。

## 桌面边界

React 前端不具有通用文件系统或 Shell 权限。目录选择由 Tauri 插件完成；Rust 后端只接受关闭式
命令，并启动打包在应用资源目录中的固定 Python sidecar。sidecar 通过标准输入接收单次 JSON
请求，以 JSONL 返回进度和终态。

桌面构建会用 PyInstaller 把 CPython 和所需模块冻结为 Windows 可执行文件。仓库不提交生成的
sidecar、Node 依赖或 Rust 构建目录。

## 验证

测试分为四组：

- Python `unittest`：核心数据结构、适配器、安全边界和自动流程；
- Vitest：前端状态和工作流；
- Cargo tests：Tauri 命令与 sidecar 桥；
- 构建冒烟测试：冻结 sidecar、凭据隐藏和关闭式协议。

CI 在 Windows 上运行上述测试、构建 Tauri 工作台、编译 Python 源码并检查空白错误。

# GalTrans

GalTrans 是一个面向 Windows 的实验性视觉小说本地化工具。它目前聚焦 Ren'Py
源码项目：从 `.rpy/.rpym` 提取可翻译文本，调用用户配置的 OpenAI 兼容服务生成译文，
校验脚本结构，并在原项目之外生成独立的 `game/tl/<language>` 翻译目录。

> **项目状态：停止主动开发。** 当前版本为 V0.4.4，代码和文档按现状公开，可能不会继续增加
> 功能、处理兼容性问题或及时回复 Issue。它是开发者工具和技术原型，不是面向普通玩家的完整
> 汉化器。

## 主要功能

- 只读扫描目录，识别常见文本文件、编码和 Ren'Py 脚本。
- 从 Ren'Py 源码提取角色台词、旁白和菜单选项，并记录来源位置与 SHA-256。
- 使用官方 Ren'Py SDK 在临时副本中生成模板、执行 lint、compile 和基础启动验证。
- 通过 OpenAI 兼容 Chat Completions 接口批量翻译，并保存可恢复的 SQLite 任务状态。
- 校验翻译响应的结构、来源身份、变量插值和 Ren'Py 文本标签。
- 只向全新输出目录发布结果，不覆盖原游戏或已有输出。
- 提供 React、TypeScript 和 Tauri 2 编写的实验性 Windows 工作台。

## 支持范围

| 输入 | 当前状态 |
| --- | --- |
| 含 `.rpy/.rpym` 的 Ren'Py 源码项目 | 支持自动翻译和验证 |
| 普通 `RPA-3.0` 中含原始 `.rpy/.rpym` | 有只读内部导入器，但尚未接入界面或 CLI |
| 只有 `.rpyc/.rpymc` 的发行版游戏 | 不支持；不会反编译 |
| 加密、受保护或自定义归档 | 不支持 |
| Ren'Py 之外的游戏引擎 | 不支持 |

GalTrans 不会自动下载 Ren'Py SDK，不会安装补丁，不会生成完整游戏副本，也不会保证未经人工
复核的翻译在语义、文风、字体或界面布局上正确。

## 快速开始

核心命令要求 Windows 和 Python 3.13。仓库运行时只使用 Python 标准库。

```powershell
py -3.13 -m venv .venv
.\scripts\galtrans.ps1 doctor
.\scripts\galtrans.ps1 scan .\samples\renpy_demo
.\scripts\galtrans.ps1 extract-renpy .\samples\renpy_demo
```

执行自动翻译还需要单独下载 Ren'Py SDK，并配置一个用户有权使用的 OpenAI 兼容服务：

```powershell
$env:GALTRANS_API_ENDPOINT = "https://provider.example/v1/chat/completions"
$env:GALTRANS_MODEL = "provider-model-name"
$env:GALTRANS_API_KEY = Read-Host "API key"

.\scripts\galtrans.ps1 translate-renpy `
    D:\path\to\renpy-sdk `
    D:\path\to\source-project `
    D:\path\to\new-output

Remove-Item Env:GALTRANS_API_KEY
```

目标输出目录必须尚不存在。完整的 CLI 和桌面构建说明见
[`docs/usage.md`](docs/usage.md)。

## 安全与隐私

- 原始项目和已安装 SDK 不会作为可写工作目录使用；可能写入的操作在系统临时副本中执行。
- API key 不写入任务数据库或输出，但待翻译文本会发送到你配置的 Provider。
- 请先阅读 Provider 的隐私、数据保留和费用条款。
- 只处理你拥有或已获授权处理的内容，并遵守游戏作者、发行平台和所在地法律的要求。
- 仓库不包含第三方游戏、脚本或译文，也不提供面向受保护内容的解包或反编译工具；样例内容为
  项目自制。

安全问题的报告方式见 [`SECURITY.md`](SECURITY.md)。

## 从源码运行桌面工作台

桌面工作台没有安装器。构建需要 Python 3.13、Node.js、Rust MSVC 工具链和 Tauri 在 Windows
上的系统依赖。

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-build.txt
Set-Location .\desktop
npm ci
npm run tauri dev
```

构建 release：

```powershell
Set-Location .\desktop
npm run tauri build
```

生成的 `galtrans-desktop.exe` 必须与构建出的 `galtrans-backend.exe` sidecar 保持在同一目录。

## 开发

```powershell
.\scripts\test.ps1
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

前端和 Rust 测试命令、代码结构及提交注意事项见
[`CONTRIBUTING.md`](CONTRIBUTING.md) 和
[`docs/architecture.md`](docs/architecture.md)。

## 许可证

本仓库目前**没有附带开源许可证**。公开代码不等于授予复制、修改或再分发许可；在维护者明确
选择并添加许可证之前，默认保留全部权利。如果计划接受外部使用或贡献，应先补充合适的
`LICENSE` 文件。

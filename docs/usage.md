# 使用指南

本文档面向希望从源码试用 GalTrans V0.4.4 的开发者。当前项目停止主动开发，未提供安装器或
稳定发行包。

## 环境准备

核心 CLI 需要：

- Windows 10 或 Windows 11；
- Python 3.13；
- PowerShell。

在仓库根目录创建虚拟环境：

```powershell
py -3.13 -m venv .venv
.\scripts\galtrans.ps1 doctor
```

核心运行时没有第三方 Python 依赖。`requirements-build.txt` 只用于构建桌面 sidecar。

## 查看命令

```powershell
.\scripts\galtrans.ps1 --help
.\scripts\galtrans.ps1 <command> --help
```

所有路径都建议使用绝对路径。写出型命令默认拒绝覆盖已有文件或目录。

## 扫描目录

```powershell
.\scripts\galtrans.ps1 scan D:\path\to\project
.\scripts\galtrans.ps1 scan D:\path\to\project --json
```

扫描只读取目录内容，报告识别到的脚本、编码、大小、行数和警告。它不会解包归档，也不会修改
输入目录。

仓库带有一个自制样例：

```powershell
.\scripts\galtrans.ps1 scan .\samples\renpy_demo
```

## 提取 Ren'Py 文本

可以输入单个 `.rpy/.rpym` 文件，也可以输入含多个脚本的项目目录：

```powershell
.\scripts\galtrans.ps1 extract-renpy .\samples\renpy_demo
.\scripts\galtrans.ps1 extract-renpy .\samples\renpy_demo `
    --output .\galtrans-output\renpy-demo.jsonl
```

JSONL 记录包含源文件相对路径、编码、SHA-256、行号、场景标签、说话人、原文和受保护标记。
输出文件已经存在时，命令会停止，不会覆盖。

提取器只保守处理常见台词、旁白和菜单写法。复杂或含糊语法会产生警告或被跳过。

## 使用 Ren'Py SDK 交叉检查

从 [Ren'Py 官方网站](https://www.renpy.org/) 获取与你的项目兼容的 SDK，然后运行：

```powershell
.\scripts\galtrans.ps1 check-renpy-sdk `
    D:\path\to\renpy-sdk `
    D:\path\to\source-project
```

GalTrans 会把源码复制到系统临时目录，在副本上调用 SDK 的 `translate` 和 `lint`，再比较自身
提取结果与官方翻译模板。原项目不会交给可能写入文件的 SDK 进程。

可用 `--language <name>` 指定 Ren'Py 语言名，默认是 `schinese`；`--json` 输出机器可读报告。

## 自动翻译

自动流程只接受含 `.rpy/.rpym` 的源码项目。你需要提供：

1. 兼容的 Ren'Py SDK；
2. 源码项目；
3. 一个尚不存在的输出目录；
4. OpenAI 兼容 Chat Completions endpoint、模型名和 API key。

```powershell
$env:GALTRANS_API_ENDPOINT = "https://provider.example/v1/chat/completions"
$env:GALTRANS_MODEL = "provider-model-name"
$env:GALTRANS_API_KEY = Read-Host "API key"

.\scripts\galtrans.ps1 translate-renpy `
    D:\path\to\renpy-sdk `
    D:\path\to\source-project `
    D:\path\to\new-output `
    --source-language ja `
    --language schinese

Remove-Item Env:GALTRANS_API_KEY
```

远程 endpoint 必须使用 HTTPS；回环地址上的本地测试服务可以使用 HTTP。API key 从环境变量读取，
不写入 SQLite 或输出文件。文本段及有限上下文会发送到所选 Provider，因此必须确认你有权处理
这些文本，并接受服务方的数据与费用条款。

默认任务工作区位于输出目录旁的 `.<输出目录名>.galtrans`。相同工作区可以恢复已保存批次；
`--workspace` 可指定其他位置。Provider 明确失败时默认最多尝试两次，网络结果无法确认时会停止，
避免重复请求和费用。

流程依次执行 SDK 交叉检查、批次翻译、结构校验、质量检查、渲染、lint 和 compile，全部通过后
才发布输出。当前质量检查无法替代人工审校。

## 验证导出

验证已有 `game/tl/<language>` 目录：

```powershell
.\scripts\galtrans.ps1 validate-renpy-export `
    D:\path\to\renpy-sdk `
    D:\path\to\source-project `
    D:\path\to\export
```

命令在相互隔离的 SDK、项目和导出副本上执行 lint 与 compile。三个输入路径必须彼此独立。

在有 Windows 图形桌面的环境中，还可以检查游戏是否出现稳定的可见窗口：

```powershell
.\scripts\galtrans.ps1 validate-renpy-launch `
    D:\path\to\renpy-sdk `
    D:\path\to\source-project `
    D:\path\to\export `
    --timeout 30
```

启动成功只证明目标语言配置下出现了可见窗口，不证明译文正确、字体完整、布局正常或所有路线
可玩。

## 图形界面

轻量 Tkinter 界面：

```powershell
.\scripts\galtrans-gui.ps1
.\scripts\galtrans-gui.ps1 --check
```

现代 Tauri 工作台从源码启动：

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-build.txt
Set-Location .\desktop
npm ci
npm run tauri dev
```

工作台会先生成并验证 Python sidecar，然后打开目录选择、兼容性检查和自动翻译界面。当前只有
`source_ready` 项目能够开始翻译；识别到 `.rpa/.rpyc` 的成品结构会显示为不支持继续。

## 已知限制

- 不反编译 `.rpyc/.rpymc`。
- 普通 RPA 源码导入器尚未连接 CLI 或界面。
- 不支持加密、受保护、自定义归档或非 Ren'Py 引擎。
- 不自动安装补丁、启动最终游戏、下载 SDK 或生成安装器。
- 不提供术语表、翻译记忆、费用统计、字体检查、截图比较或完整人工审校工作流。
- SQLite 旧结构不会自动迁移。

# GalTrans

GalTrans 的长期目标是让普通玩家把自己合法持有、且被工具明确支持的生肉视觉小说，便捷地
制作成可以像已有汉化游戏一样启动和游玩的独立个人汉化副本。原游戏目录保持只读，语言模型
只能提交结构化翻译建议，确定性程序负责游戏文件处理、校验和导出。

V0.4.1 已把第一个真实 OpenAI 兼容 Provider、可恢复翻译状态、确定性质量检查、Ren'Py 渲染和
隔离验证连接成自动流程，并增加 TypeScript、React 和 Tauri 2 构建的现代 Windows 玩家工作台。
这一版还新增只读 Ren'Py 兼容性检查：可以区分当前可处理的源码项目、已识别但尚不能导入的
`.rpa/.rpyc` 成品结构、证据不足的目录和非 Ren'Py 目录。它不会打开归档或反编译脚本；成品游戏
导入与 Windows 安装包仍未实现。

V0.2 已支持只读扫描、Ren'Py 文本提取，以及在临时副本上调用官方 SDK 完成导出和基础显示验证：

- 发现常见视觉小说脚本和文本文件；
- 检测 UTF-8、UTF-16、CP932/Shift-JIS 和 GB18030；
- 计算文件哈希，为后续安全回写建立基线；
- 提取角色台词、旁白和菜单选项，并保护变量与文本标签；
- 为已逐条匹配的简单文本生成官方翻译片段，并组装到全新的独立补丁目录；
- 在完整可写的临时 SDK 与项目副本上 lint 并独立编译已导出的翻译目录；
- 只在相同隔离边界中启动导出，观察真实可见窗口并收口整个进程树；
- 不解包，也不修改任何输入游戏文件。

V0.3 的第一个小里程碑已经定义应用自有的结构化翻译任务边界：从统一文本段生成稳定的任务和
批次 ID，只把筛选后的文本、有限上下文、来源摘要和受保护标记交给后端；严格验证返回提案的
schema、来源和 Ren'Py 标记；并用可序列化检查点表达暂停、恢复、失败、重试和幂等重放。当前
还可以在输入项目之外的 SQLite 数据库中原子保存任务、检查点和已接受提案，并在重开后恢复，
同时拒绝旧检查点覆盖新进度。内部单批次执行器现在可以把确定性后端、提案验证和 SQLite 状态
连接成可暂停、失败、重试和重开继续的流程；稳定请求 ID 和 SQLite 结果缓存还能在已验证响应
落盘后避免崩溃重放再次调用同一后端配置。现有请求 ID 现在也是 Provider 幂等键，SQLite 会保存
`in_flight`、`succeeded`、`failed` 或 `unknown` 调用回执；执行器可以在重开后按幂等键查询在途或
未知请求，只对确定失败进行显式受控重试，无法消歧时安全停止。V0.3 现在包含第一个真实网络
适配器：它通过用户配置的 OpenAI 兼容 Chat Completions HTTPS endpoint 请求 JSON 译文，只发送
筛选后的批次，不发送项目路径或 API key 到任务存储。同步兼容协议没有可靠的通用查询接口，
因此网络结果不确定时不会盲目重提。已经过引擎校验的完整任务还可以生成关闭式、
版本化的纯内存质量报告：首条确定性规则会把 Unicode NFC 规范化后仍与原文完全相同的译文列入
`low_confidence` 复核结果；没有命中只表示当前规则未发现问题，不代表译文质量已经得到保证。
完成任务的质量报告现在还能绑定检查点真正接受的提案原子保存到 SQLite；重开时会用当前 Ren'Py
验证器和已接受提案重新计算，不能用调用方自报的 `clear` 覆盖需要复核的结果。自动命令会在
Provider 调用前完成 SDK 交叉检查，译文必须同时通过标记验证和纯内存渲染验证；生成物先写入
临时目录，只有 SDK lint 与独立 compile 通过后才发布到用户指定的全新输出目录。

## 本地运行

项目使用现有的 Python 3.13，并在 `.venv` 中隔离运行环境。

```powershell
.\scripts\galtrans.ps1 doctor
.\scripts\galtrans.ps1 scan D:\path\to\game
.\scripts\galtrans.ps1 scan D:\path\to\game --json
```

仓库内含一个完全自制的 Ren'Py 小样例，可以直接试运行扫描：

```powershell
.\scripts\galtrans.ps1 scan .\samples\renpy_demo
```

现代 Windows 工作台目前从仓库开发环境启动：

```powershell
Set-Location .\desktop
npm ci
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
npm run tauri dev
```

窗口中选择 Ren'Py SDK、带 `game` 目录的源项目和一个尚不存在的输出路径，再填写翻译服务
（Provider）的 Chat Completions URL、模型名和 API key，点击“开始自动汉化”。窗口会显示路径
检查、提取、SDK 交叉检查、批次翻译、质量检查、渲染、导出验证和发布进度；成功或失败后输入
控件都会恢复。TypeScript 界面只通过关闭式 JSON/JSONL 桥调用现有 Python 自动服务，不复制
游戏处理逻辑。API key 通过标准输入交给固定 Python 子进程，不进入命令行、环境变量、工作区、
日志或 SDK 参数；开始后输入框立即清空。

首次开发启动需要 Node.js、Rust 和项目内 npm 依赖。当前界面仍依赖仓库内的 Python 环境和单独
安装的 Ren'Py SDK，不是安装包；它不会自动下载 SDK、启动游戏或把补丁拼成完整游戏副本。
原有 Tkinter 界面暂时保留为轻量回退入口：

```powershell
.\scripts\galtrans-gui.ps1
```

可以用下面的命令只检查 Tkinter 图形环境而不打开窗口：

```powershell
.\scripts\galtrans-gui.ps1 --check
```

从样例中提取角色台词、旁白和菜单选项，并输出 JSONL：

```powershell
.\scripts\galtrans.ps1 extract-renpy .\samples\renpy_demo\game\script.rpy
.\scripts\galtrans.ps1 extract-renpy .\samples\renpy_demo\game\script.rpy `
    --output .\galtrans-output\renpy-demo.jsonl
```

也可以直接传入包含多个脚本的 Ren'Py 项目目录：

```powershell
.\scripts\galtrans.ps1 extract-renpy .\samples\renpy_demo
```

为避免意外丢失审阅结果，输出文件已存在时命令会拒绝覆盖。

每条 JSONL 记录包含格式版本、项目内来源路径、源编码、源文件 SHA-256、场景标签、行号、
说话人、原文以及受保护标记。后续翻译任务可以据此验证自己处理的是正确版本。

验证本机 Ren'Py SDK，并与官方翻译模板逐条交叉检查：

```powershell
.\scripts\galtrans.ps1 check-renpy-sdk D:\path\to\renpy-sdk .\samples\renpy_demo
```

该命令会定位 SDK 的真实 `renpy.exe`，验证版本，只把项目 `game` 目录中的 `.rpy/.rpym`
源文件复制到系统临时目录，然后在临时副本上运行官方 `translate schinese` 和 `lint`。
输入项目不会交给 SDK，也不会产生 `tl`、缓存、存档或编译文件。GalTrans 会比较自身提取的
文本段与官方模板记录的源文件、行号、类型和原文，并保留官方翻译 ID。数量相同但文本不同
也会失败；不一致或遇到暂不支持的复杂模板块时返回退出码 4，供人工检查。添加 `--json`
可以查看逐条映射、未匹配项目和警告。

自动翻译一个 source-only Ren'Py 项目：

```powershell
$env:GALTRANS_API_ENDPOINT = "https://provider.example/v1/chat/completions"
$env:GALTRANS_MODEL = "provider-model-name"
$env:GALTRANS_API_KEY = Read-Host "API key"

.\scripts\galtrans.ps1 translate-renpy `
    D:\path\to\renpy-sdk `
    D:\path\to\source-project `
    D:\path\to\new-translated-output

Remove-Item Env:GALTRANS_API_KEY
```

endpoint 和模型名必须按所选服务的文档填写；远程 endpoint 必须使用 HTTPS。本机测试服务可以
使用回环 HTTP。API key 只从指定环境变量读取，不接受命令行参数，也不会保存进 SQLite 或输出
报告；调用 Ren'Py SDK 前还会从子进程环境中临时移除该变量。默认工作区位于输出目录旁的
`.<输出名>.galtrans`；可用 `--workspace` 指定另一个输入项目之外的目录。相同工作区重跑会恢复
已保存批次；明确失败最多尝试两次，结果不确定时停止，避免可能的重复费用。

该命令不要求逐条人工审阅。`low_confidence` 会在结果中报告但不会阻止独立输出；这提高了自动化
程度，也意味着错译、原文残留、术语和文风问题可能进入成品。最终目录不存在时才会发布，已有
目录始终拒绝覆盖。当前命令执行 SDK 交叉检查、翻译、质量报告、lint 和 compile，尚不自动启动
游戏；可以继续用下面的启动验证命令检查窗口。

验证一个已经独立导出的 Ren'Py 翻译目录：

```powershell
.\scripts\galtrans.ps1 validate-renpy-export `
    D:\path\to\renpy-sdk `
    .\samples\renpy_demo `
    D:\path\to\export
```

该命令要求 SDK、输入项目和导出根目录相互独立。它会在系统临时目录中完整复制 SDK，只复制
项目的 `.rpy/.rpym` 源脚本，再合并导出根目录中的 `game/tl/schinese`。lint 完成后会清除
临时项目的编译物并单独运行 compile，要求每个项目与翻译脚本都生成对应编译文件。已安装 SDK、
输入项目和导出目录不会交给可能写入的 lint/compile 进程，所有临时产物在结束后清理。

在有 Windows 图形桌面的本机验证导出的基础启动和显示：

```powershell
.\scripts\galtrans.ps1 validate-renpy-launch `
    D:\path\to\renpy-sdk `
    .\samples\renpy_demo `
    D:\path\to\export
```

该命令复用导出验证的完整临时 SDK、source-only 项目副本和导出合并边界，并设置目标
`RENPY_LANGUAGE`。唯一的启动命令是临时 `renpy.exe <临时项目> run --savedir <临时存档>`。
只有属于该进程、连续稳定可见且客户区非零的顶层窗口才是基础显示证据；错误窗口、提前正常
退出、非零退出或超时都不算通过。显示成功后，命令请求无确认的 Ren'Py 正常关闭；若未按时
退出则依次 terminate、kill，最后由 Windows Job Object 终止并确认进程树清空。

stdout/stderr、Ren'Py 日志、缓存、编译文件、存档和可能的截图都只允许出现在系统临时根中的
SDK、项目、存档或隔离用户数据目录，结束后统一清理。`--timeout` 可以调整等待窗口的秒数。
此命令需要真实 Windows 图形桌面；CI 使用受控进程、窗口和 Job Object 替身测试边界，不启动
真实 SDK。

运行标准库测试：

```powershell
.\scripts\test.ps1
```

## 当前范围

内部 `inspect_renpy_compatibility` 接口会在有界目录扫描中列出松散源脚本、编译脚本、RPA 归档、
已有 `game/tl` 翻译文件、根目录启动器、运行时目录和可安全读取的版本线索。报告使用关闭式 v1
结构，只在完整扫描确实发现 `.rpy/.rpym` 时返回 `source_ready`；标准成品结构返回
`packaged_requires_import`，不完整或相互矛盾的证据返回 `uncertain`。该接口目前尚未连接 CLI
或桌面工作台，也没有因此获得解包、反编译、复制或启动游戏的能力。

Ren'Py 保守提取器已经可以把 `.rpy` 中的常见角色台词、旁白和菜单选项转换为稳定文本段，
同时标记变量插值、文本标签和转义内容。SDK 交叉验证可以把自制源项目中的简单单语句台词、
旁白和菜单逐条对应到官方模板，但还不是完整 Ren'Py 语法等价验证。复杂翻译块会被警告并
跳过。确定性导出后端会保留官方原语句结构、校验受保护标记，并且只写全新的独立
`game/tl/<language>` 补丁目录；导出目录可以在完整可写的临时 SDK 与 source-only 项目副本中
通过 lint、独立 compile 和基础启动。基础显示只证明目标语言启动时出现真实非零窗口，不检查
具体像素、译文内容、字体、溢出或交互路线。自动流程现在可以通过第一个 OpenAI 兼容网络适配器
处理任务，并把通过确定性验证的译文发布到全新输出目录；网络协议由本机 HTTP 服务完整测试，
尚未用用户的真实商业 Provider、凭据或费用进行实机验证，也不承诺所有标称“OpenAI 兼容”的
服务行为完全一致。当前没有费用统计、翻译记忆、审计、角色卡、术语表、自动语义审校、安装包
或自动启动。现代 Tauri 工作台和 Tkinter 回退界面都只连接现有 source-only 自动流程；当前开发
版本尚未把 Python 打包为 sidecar、自动发现 SDK 或支持普通成品游戏。首条质量检查只覆盖“译文
与原文未变化”；低置信度不会阻塞自动输出，因此本版本强调安全生成与可恢复性，不保证无人
审阅译文的文学质量或语义正确性。SQLite v1 至 v3 继续明确拒绝，不进行自动迁移。

具体进度见 [`docs/roadmap.md`](docs/roadmap.md)。

## 安全原则

1. 默认只读扫描。
2. 原始游戏文件不被覆盖。
3. 每条文本具有稳定来源位置和哈希。
4. 模型输出必须经过结构与格式校验后才能进入导出流程。
5. 测试样例只使用自制或得到授权的内容。
6. API key 只在调用进程内存和 HTTPS 请求头中使用，不写入项目、SQLite 或输出。

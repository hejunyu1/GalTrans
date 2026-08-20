# GalTrans

GalTrans 的长期目标是让普通玩家把自己合法持有、且被工具明确支持的生肉视觉小说，便捷地
制作成可以像已有汉化游戏一样启动和游玩的独立个人汉化副本。原游戏目录保持只读，语言模型
只能提交结构化翻译建议，确定性程序负责游戏文件处理、校验和导出。

V0.2 已建立 Ren'Py 可运行闭环，当前开始 V0.3 的翻译工作流基础；仍只支持带源脚本的 Ren'Py
项目，尚未提供普通成品游戏导入、自动模型翻译、玩家 GUI 或一键个人汉化。遇到加密、受保护
或无法可靠识别的格式时，GalTrans 的方向是明确提示不支持，而不是破解或猜测处理。

V0.2 已支持只读扫描、Ren'Py 文本提取，以及在临时副本上调用官方 SDK 完成导出和基础显示验证：

- 发现常见视觉小说脚本和文本文件；
- 检测 UTF-8、UTF-16、CP932/Shift-JIS 和 GB18030；
- 计算文件哈希，为后续安全回写建立基线；
- 提取角色台词、旁白和菜单选项，并保护变量与文本标签；
- 为已逐条匹配的简单文本生成官方翻译片段，并组装到全新的独立补丁目录；
- 在完整可写的临时 SDK 与项目副本上 lint 并独立编译已导出的翻译目录；
- 只在相同隔离边界中启动导出，观察真实可见窗口并收口整个进程树；
- 不解包、不自动翻译，也不修改任何输入游戏文件。

V0.3 的第一个小里程碑已经定义应用自有的结构化翻译任务边界：从统一文本段生成稳定的任务和
批次 ID，只把筛选后的文本、有限上下文、来源摘要和受保护标记交给后端；严格验证返回提案的
schema、来源和 Ren'Py 标记；并用可序列化检查点表达暂停、恢复、失败、重试和幂等重放。当前
还可以在输入项目之外的 SQLite 数据库中原子保存任务、检查点和已接受提案，并在重开后恢复，
同时拒绝旧检查点覆盖新进度。内部单批次执行器现在可以把确定性后端、提案验证和 SQLite 状态
连接成可暂停、失败、重试和重开继续的流程；已经完成的批次不会再次调用后端。当前仍只有测试
内的确定性后端替身，没有真实模型、请求缓存、费用计算或翻译命令；通过校验的结果不会自动
进入渲染、导出或启动边界。

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

Ren'Py 保守提取器已经可以把 `.rpy` 中的常见角色台词、旁白和菜单选项转换为稳定文本段，
同时标记变量插值、文本标签和转义内容。SDK 交叉验证可以把自制源项目中的简单单语句台词、
旁白和菜单逐条对应到官方模板，但还不是完整 Ren'Py 语法等价验证。复杂翻译块会被警告并
跳过。确定性导出后端会保留官方原语句结构、校验受保护标记，并且只写全新的独立
`game/tl/<language>` 补丁目录；导出目录可以在完整可写的临时 SDK 与 source-only 项目副本中
通过 lint、独立 compile 和基础启动。基础显示只证明目标语言启动时出现真实非零窗口，不检查
具体像素、译文内容、字体、溢出或交互路线。当前仍没有接入译文输入命令或真实模型。SQLite
存储已经能原子保存任务、检查点和已接受提案，内部执行器也能每次安全处理一个批次，但尚未
定义玩家工作区、请求缓存、审计、费用记录、真实模型进程或翻译命令，因此不能把它描述成已经
完成面向玩家的断点续传工作流。

具体进度见 [`docs/roadmap.md`](docs/roadmap.md)。

## 安全原则

1. 默认只读扫描。
2. 原始游戏文件不被覆盖。
3. 每条文本具有稳定来源位置和哈希。
4. 模型输出必须经过结构与格式校验后才能进入导出流程。
5. 测试样例只使用自制或得到授权的内容。

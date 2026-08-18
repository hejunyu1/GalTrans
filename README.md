# GalTrans

GalTrans 是一个面向 Galgame 和其他视觉小说的安全、可审阅汉化工作流。

当前版本已经支持只读扫描、Ren'Py 文本提取，以及在临时副本上调用官方 SDK 做交叉验证：

- 发现常见视觉小说脚本和文本文件；
- 检测 UTF-8、UTF-16、CP932/Shift-JIS 和 GB18030；
- 计算文件哈希，为后续安全回写建立基线；
- 提取角色台词、旁白和菜单选项，并保护变量与文本标签；
- 为已逐条匹配的简单文本生成官方翻译片段，并组装到全新的独立补丁目录；
- 在完整可写的临时 SDK 与项目副本上 lint 并独立编译已导出的翻译目录；
- 不解包、不自动翻译，也不修改任何输入游戏文件。

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
通过 lint 和独立 compile。当前还没有接入译文输入命令或模型，也不启动图形界面或验证显示
效果。

具体进度见 [`docs/roadmap.md`](docs/roadmap.md)。

## 安全原则

1. 默认只读扫描。
2. 原始游戏文件不被覆盖。
3. 每条文本具有稳定来源位置和哈希。
4. 模型输出必须经过结构与格式校验后才能进入导出流程。
5. 测试样例只使用自制或得到授权的内容。

# GalTrans

GalTrans 是一个面向 Galgame 和其他视觉小说的安全、可审阅汉化工作流。

当前版本是最小工程骨架，只做只读扫描：

- 发现常见视觉小说脚本和文本文件；
- 检测 UTF-8、UTF-16、CP932/Shift-JIS 和 GB18030；
- 计算文件哈希，为后续安全回写建立基线；
- 不解包、不翻译，也不修改任何游戏文件。

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

运行标准库测试：

```powershell
.\scripts\test.ps1
```

## 当前范围

Ren'Py 保守提取器已经可以把 `.rpy` 中的常见角色台词、旁白和菜单选项转换为稳定文本段，
同时标记变量插值、文本标签和转义内容。它暂时不是完整 Ren'Py 语法解析器；不确定的写法会被
跳过或报告，之后将通过 Ren'Py SDK 做官方交叉验证。

具体进度见 [`docs/roadmap.md`](docs/roadmap.md)。

## 安全原则

1. 默认只读扫描。
2. 原始游戏文件不被覆盖。
3. 每条文本具有稳定来源位置和哈希。
4. 模型输出必须经过结构与格式校验后才能进入导出流程。
5. 测试样例只使用自制或得到授权的内容。

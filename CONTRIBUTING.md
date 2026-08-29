# 贡献指南

感谢你关注 GalTrans。项目目前停止主动开发，Issue 和 Pull Request 可能不会得到及时回复或合并。

## 开发环境

核心项目使用 Windows 和 Python 3.13：

```powershell
py -3.13 -m venv .venv
.\scripts\galtrans.ps1 doctor
.\scripts\test.ps1
```

桌面部分还需要 Node.js、Rust MSVC 工具链及 Tauri 的 Windows 系统依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-build.txt
Set-Location .\desktop
npm ci
npm test
npm run build
cargo test --manifest-path .\src-tauri\Cargo.toml
```

## 贡献边界

- 不要提交受版权保护、来源不明或未经授权的游戏文件、脚本、译文、SDK 或模型凭据。
- 测试夹具必须是自行创作、明确授权或足够小的合成数据。
- 不要让模型、Provider 或前端直接修改输入游戏。
- 不要添加绕过加密、DRM、访问控制或反编译编译脚本的功能。
- 新写出路径必须默认拒绝覆盖已有内容。
- 解析含糊语法时，应返回警告或不支持，而不是猜测。

## 提交前检查

Python：

```powershell
.\scripts\test.ps1
.\.venv\Scripts\python.exe -m compileall -q src tests
```

桌面：

```powershell
Set-Location .\desktop
npm test
npm run build
cargo test --manifest-path .\src-tauri\Cargo.toml
```

最后回到仓库根目录运行：

```powershell
git diff --check
```

行为变化应配套测试，并在 README 或 `docs/` 中更新面向用户的限制说明。提交应保持单一目的，
不要夹带生成目录、虚拟环境、凭据或第三方游戏内容。

## 许可证说明

仓库目前没有开源许可证。在许可证确定前，请不要假定你提交的代码或现有代码可以被自由复制、
修改或再分发。计划进行较大贡献前，建议先通过 Issue 与维护者确认许可证和接受贡献的安排。

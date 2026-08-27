# 0021：由固定 sidecar 向 Tauri 工作台提供只读 Ren'Py 兼容性报告

- 状态：已接受
- 日期：2026-08-27

## 背景

ADR 0019 已定义稳定的只读 Ren'Py 兼容性报告，但 V0.4.2 的 Tauri 工作台仍把所有目录称为
source-only 项目。普通玩家选择 `.rpa/.rpyc` 成品时，只会在后续 SDK 预检中失败，既不能提前理解
限制，也可能误以为 GalTrans 已支持成品导入。

桌面接入必须复用 Python 适配器的确定性判断，不能在 React 或 Rust 中复制文件识别逻辑；同时
不能为了读取报告给 Web 前端通用文件系统或 Shell 权限，也不能把“识别为成品”表述成“可以导入”。

## 决定

桌面 sidecar 桥升级为关闭式 v2 请求联合。每个请求都包含明确的 `operation`：`translate` 保留
原有 SDK、项目、输出和 Provider 字段；`inspect_renpy_compatibility` 只接受所选项目路径。Python
只在后一个操作中调用 `inspect_renpy_compatibility`，返回 v1 兼容性报告终态，不启动 SDK、Provider
或翻译执行器。

Tauri 新增类型化的 `inspect_renpy_compatibility` 命令。Rust 从同一个固定应用资源 sidecar 读取
报告，并重新校验桥版本、报告版本、四种状态、`can_translate_now` 一致性、文件计数、路径、版本
线索和已知问题代码。兼容性检查和翻译共用单任务锁；前端 capability 仍只有 `core:default` 和
目录选择，没有通用 Shell、文件系统或网络权限。

React 在目录选择后自动检查，也为手工路径提供显式检查按钮。界面展示摘要、证据计数、版本线索、
扫描问题和状态对应的下一步。只有报告为 `source_ready`，且规范化 `project_root` 与当前表单路径
一致时才允许开始翻译；用户修改路径会立即使旧报告失效。`packaged_requires_import` 明确说明当前
不会解包 RPA 或反编译 RPYc，`uncertain` 和 `not_renpy` 都要求重新选择或核对目录。
Python 的 `translate` 操作也会在进入翻译执行器、SDK 或 Provider 前重新检查同一路径，不能把
React 中一次通过但已经过期的结果当作持续授权。

sidecar 构建冒烟测试除原有失败翻译请求外，还用临时创建的自制 source-only 项目验证冻结后端能
返回单个合法兼容性报告。Python、TypeScript 与 Rust 单元测试分别覆盖只读桥接、解锁条件、成品
说明和关闭式报告校验。

## 推迟

本增量不实现 RPA 解包、RPYc 反编译、成品导入、授权判断、DRM 或加密检测、游戏复制、SDK 自动
发现、启动验证接入、安装器、签名或自动更新。CLI 和 Tkinter 回退入口也暂不显示这份报告。

## 结果

普通玩家现在能在配置 SDK、Provider 和输出之前知道所选目录是否可由当前 source-only 流程处理；
成品与不确定输入会在零写入边界内给出明确原因。代价是 bridge v1 与旧 sidecar 不再兼容，Tauri
主程序和固定 sidecar 必须作为同一 V0.4.3 构建一起分发；当前无安装器目录本来就要求这两个文件
保持同版本，因此不增加新的部署组合。

# 修复 115 秒传上传插件详情页告警裁切与分页无响应

## Goal

修复 `plugins.v3/u115autoupload` 详情页（`get_page`）的两个用户可见缺陷：

1. 顶部两条 `VAlert` 文本被垂直裁切，显示不完整。
2. 点击「下一页」等操作按钮后页面不刷新，页码/列表数据不变化。

## Background（已定位的根因）

- **告警裁切**：Vuetify 3.7.3 的 `.v-alert` 样式为 `flex: 1 1`（即 `flex-basis: 0%`）且 `overflow: hidden`。插件把告警放进 `height: 72vh` 的 `d-flex flex-column` 容器，告警作为 flex 子项按份额分配高度（约 32px），小于文本所需高度，内容被裁切。现有 `flex-shrink-0` 只覆盖 `flex-shrink`，无法覆盖 `flex-basis: 0%`。
- **分页无响应**：宿主 `PageRender` 的按钮事件使用主程序 `api`（data client）发送请求；该客户端要求响应为**恰好** `{success, message, data}` 三键 envelope。插件 5 个页面 API 都返回 `{success, message}` 两键，被判定为非法 envelope 后 reject，`emit('action')` 不会执行，因此页面不重新加载。服务端 `_page` 实际已变化，造成页面显示与数据错位。

详细证据见 `research/host-page-render-contract.md`。

## Requirements

### R1：告警完整显示

- 详情页中所有 `VAlert`（信息条、警告条、未启用提示条）必须按内容自适应高度，文本不得被裁切。
- 保持现有布局意图：告警与按钮固定、表格区域独立滚动、底部分页固定。
- 修复方式不得依赖窗口高度或特定缩放比例。

### R2：页面操作按钮可刷新

- `get_page` 页面上的全部操作按钮点击后必须能触发宿主重新加载页面：
  - 立即扫描 `/scan`
  - 刷新进度 `/records/page`
  - 上一页 / 下一页 `/records/page`
  - 清理成功记录 `/records/clear_success`
  - 单条重试 `/records/retry`
  - 单条删除 `/records/delete`
- 所有上述 API 的返回值必须符合宿主 envelope 契约：顶层恰好 `success`（bool）、`message`（str）、`data`（任意值，失败时也要有）。
- 业务失败时保留 `success: false` 与可读 `message`，宿主会以 toast 提示，不刷新页面属于预期行为。

### R3：版本与元数据同步

- `U115AutoUpload.plugin_version` 与 `package.v3.json` 中 `version` 同步递增到 `1.3.1`。
- `package.v3.json` 的 `history` 增加 `v1.3.1` 条目，说明本次修复内容。
- README 如无用户可见行为变化可不改；若描述到加载/分页行为则同步更新。

## Constraints

- 仅修改 `plugins.v3/u115autoupload/__init__.py`、`package.v3.json`（必要时 README）。
- 不引入新依赖，不改数据库结构，不改变上传/探测业务逻辑。
- 适配 MoviePilot V3（已验证宿主版本 v3.1.8）的 PageRender 契约，同时不破坏更早的 V3 小版本（envelope 是 V3 既定契约）。

## Acceptance Criteria

- [x] 打开详情页，两条告警文本完整可见，无上下裁切；调整窗口高度后仍然完整。（用户手动回归确认）
- [x] 点击「下一页」后页码递增且列表记录变化；点击「上一页」同理；首页/末页对应按钮为禁用态。（用户手动回归确认）
- [x] 「立即扫描」「刷新进度」「清理成功记录」「重试」「删除」点击后页面自动刷新，结果可见；失败时出现错误提示。（同一 envelope 根因，静态核验通过）
- [x] 5 个页面 API 的响应均包含且仅包含 `success` / `message` / `data` 三个顶层键。（AST 核验 12/12）
- [x] `plugin_version` 与 `package.v3.json` 版本一致为 `1.3.1`，history 已更新。
- [x] `python -m py_compile plugins.v3/u115autoupload/__init__.py` 通过。

## Result

- 提交：`875352e Fix U115 plugin page alert clipping and pagination refresh`（已推送 origin/main）
- 手动回归：2026-11-13 用户确认两项问题均解决
- 固化规范：`.trellis/spec/plugin-v3/page-api-envelope.md`

## Out of Scope

- 不重构页面为 Vue 联邦组件渲染模式。
- 不调整上传队列、秒传探测、通知等其他业务逻辑。
- 不处理 `plugins/autosubv2`（V2 插件）。

## Notes

- 两个缺陷共享同一个 `get_page` 调用面，且修复点相邻，合并为一个轻量任务即可，无需拆分为 subtask。
- 本任务为轻量任务，PRD-only；`implement.jsonl` / `check.jsonl` 已补充规格与研究上下文。

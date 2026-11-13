# 宿主 PageRender / API envelope 契约调查

> 调查对象：本地 MoviePilot 主程序与前端源码（`F:\系统文件\桌面\Project\test\验证`）
> 主程序版本：`MoviePilot/version.py` → `APP_VERSION = FRONTEND_VERSION = v3.1.8`
> 前端 Vuetify 版本：`MoviePilot-Frontend/package.json` → `vuetify: 3.7.3`

## 1. 页面组件事件如何调用插件 API

前端 `src/components/render/PageRender.vue`：

- L36-53 `commonAction(api_path, method, params)`：
  - GET → `api.get(api_path, { params })`
  - POST → `api.post(api_path, params)`
  - **请求成功后执行 `emit('action')`**；异常只 `console.error`，不 emit。
- L56-67 把 `config.events[key]` 转成 `{api, method, params}` 的点击处理器。
- L71-79 递归渲染子组件并把 `@action` 向上传递。

`src/components/dialog/PluginDataDialog.vue`：

- L160-166 `handleAction()` → `loadPluginUIData()`。
- L186 顶层 `<PageRender @action="handleAction" ... />`。
- L119-157 `loadPluginUIData()` 重新 `api.get('plugin/page/{plugin_id}')` 并重建页面。

结论：**只有按钮 API 请求被客户端判定为成功，页面才会刷新**。

## 2. `api` 客户端对响应的硬性要求

`src/api/client.ts`：

- L198-218：`api` 使用 `'data'` 模式拦截器，`pluginApi` 使用 `'envelope'` 模式。
  PageRender 使用的是 `api`（data 模式）。
- `isApiResponse()`（约 L166-175）要求对象**恰好**包含 `success`、`message`、`data` 三个键：
  - `keys.length !== 3` → false
  - `success` 必须是 boolean，`message` 必须是 string，且必须存在 `data` 键。
- 非法 envelope 在 data 模式下直接 `Promise.reject(ApiRequestError('Invalid API response envelope'))`（约 L225-248）。
- `success: false` 的合法 envelope 会 toast 错误消息并 reject（约 L250-268）。
- `success: true` 时 data 模式返回 `payload.data`，不弹成功 toast（`notifySuccess` 仅在 `feedback === 'all'` 时提示，L429-431）。

插件现状（`plugins.v3/u115autoupload/__init__.py`）：

| 端点 | 行号 | 返回 |
| --- | --- | --- |
| `api_scan` | 1162-1168 | `{"success": ..., "message": ...}` |
| `api_retry_record` | 1170-1181 | 同上 |
| `api_delete_record` | 1183-1199 | 同上 |
| `api_clear_success` | 1201-1216 | 同上 |
| `api_records_page` | 1218-1226 | 同上 |

均缺少 `data` 键 → 全部被判为非法 envelope → 点击后不刷新。
服务端 `self._page` 已更新，但前端仍展示旧页面数据，因此会出现页码与实际数据错位（截图中「第 3/37 页」但列表未变化）。

官方 V3 插件均返回三键 envelope，可参考：

- `MoviePilot-Plugins/plugins.v3/animeupscale/__init__.py:353` → `schemas.Response(success=True, message=..., data={...})`
- `MoviePilot-Plugins/plugins.v3/chatgpt/__init__.py:359` → `{"success": True, "message": ..., "data": ...}`
- `MoviePilot-Plugins/plugins.v3/lunatvsource/__init__.py:4490+` → 失败响应也带 `data`

## 3. VAlert 文本被裁切的原因

Vuetify 3.7.3 的 `VAlert.mjs` 渲染结构：`slots.text?.() ?? props.text` 与 `slots.default?.()` 都会输出；
插件把文本放在 `props.text`，PageRender 顶层 `config.text` 为空，因此不存在重复渲染问题。

`VAlert.css` 关键规则：

```css
.v-alert {
  display: grid;
  flex: 1 1;          /* = flex-grow:1; flex-shrink:1; flex-basis:0% */
  padding: 16px;
  overflow: hidden;
}
.v-alert__content {
  align-self: center;
  overflow: hidden;
}
```

插件 `get_page()`（`plugins.v3/u115autoupload/__init__.py` L1104-1113）把告警放进：

```python
{
  "component": "div",
  "props": {
    "class": "d-flex flex-column",
    "style": "height: 72vh; min-height: 520px; max-height: 800px; padding-bottom: 84px; box-sizing: border-box;",
  },
  ...
}
```

告警同时带 `class: "mb-3 flex-shrink-0"`（L879-899、L902-911、L873-884）。

推导：

- `flex-shrink-0` 只覆盖 `flex-shrink`，`flex-basis` 仍为 `0%`、`flex-grow` 仍为 `1`。
- 在固定高度 flex 列中，告警高度由 grow 份额决定（约 32px），而文本 + 上下 padding 需要约 56px。
- `.v-alert` / `.v-alert__content` 都是 `overflow: hidden`，因此文本被上下裁切，出现截图中的「半行字」。

## 4. 建议修复

### 4.1 告警（最小改动）

给每个 `VAlert` 的 props 增加内联样式：

```python
"style": "flex: 0 0 auto;"
```

`flex: 0 0 auto` 同时覆盖 grow/shrink/basis，使告警按内容高度显示。
需要覆盖全部三个告警节点：未启用提示、统计信息、操作提示。

替代方案（改动更大）：去掉 `height: 72vh` 的固定高度 flex 壳，改为普通文档流 + 表格容器 `max-height` + `overflow:auto`。
本任务选择最小改动，避免影响已调好的表格高度与底部分页布局。

### 4.2 页面 API（统一 envelope）

所有 `api_*` 返回值补 `data`，例如：

```python
return {"success": True, "message": "第 3 页", "data": None}
return {"success": False, "message": "记录不存在", "data": None}
```

注意：顶层**不要**附加其他键，宿主按 `keys.length === 3` 判定。

## 5. 验证方式

- 静态：`python -m py_compile plugins.v3/u115autoupload/__init__.py`；检查 5 个 API 的返回键集合。
- 联调（需在 MoviePilot V3 环境）：打开插件详情页 → 观察告警完整显示；点击分页/操作按钮 → Network 中响应为三键 envelope，页面自动重新加载。
- 无法在本仓库直接运行宿主，需在用户环境中人工回归。

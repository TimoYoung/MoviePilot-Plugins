# Scenario: 插件详情页按钮 API 与页面渲染契约

## 1. Scope / Trigger

适用于所有在 `get_page()` 中返回 Vuetify 组件树、并通过 `events.click` 调用插件自有 API 的 MoviePilot V3 插件。

触发条件：

- 新增或修改 `get_api()` 中的页面操作端点
- 页面按钮"点了没反应"、页码不刷新、页面不重载
- 详情页中出现 `VAlert` 文本被裁切/压扁

## 2. Signatures

### 2.1 页面按钮事件

```python
{
    "component": "VBtn",
    "text": "下一页",
    "events": {
        "click": {
            "api": f"plugin/{self.__class__.__name__}/records/page",
            "method": "post",
            "params": {"page": self._page + 1},
        }
    },
}
```

- `api` 为 `/api/v1` 之后的相对路径：`plugin/{plugin_id}/{get_api 中声明的 path}`。
- 参数统一放在 `params`；`method` 支持 `get` / `post`。

### 2.2 页面操作 API

```python
def api_records_page(self, payload: dict = Body(...)) -> Dict[str, Any]:
    return {
        "success": True,
        "message": f"第 {self._page} 页",
        "data": {"page": self._page},
    }
```

## 3. Contracts

### 3.1 响应 envelope（强制）

宿主 `PageRender` 使用主程序 `api`（data client），要求响应对象**恰好**包含三个顶层键：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `success` | bool | 必须 |
| `message` | str | 必须；失败时作为 toast 文案 |
| `data` | any | **必须存在**；无内容用 `None` |

- 键数量必须为 3：多一个或少一个都会被判定为非法 envelope。
- 成功（`success: true`）时前端才会执行 `emit('action')` → 宿主重新请求 `plugin/page/{plugin_id}` → 页面刷新。
- 失败（`success: false`）时宿主弹错误 toast 且不刷新页面。
- 成功不会弹 toast（除非请求显式设置 `feedback: 'all'`）。

参考实现：`plugins.v3/u115autoupload/__init__.py` 中 `api_scan`、`api_retry_record`、`api_delete_record`、`api_clear_success`、`api_records_page`。

### 3.2 页面状态

- `get_page()` 无请求参数；需要分页/筛选状态时，只能保存在插件实例字段中（宿主 `running_plugins` 持有同一实例），由按钮 API 修改后再触发页面重载。
- 不要在按钮事件里依赖前端本地状态，PageRender 只传 `params` 给 API。
- 按钮的 `params` 在 `get_page()` 渲染时计算，页面未刷新前重复点击会发送相同目标值，因此**必须保证 envelope 合法以触发刷新**。

### 3.3 固定高度布局中的 Vuetify 组件

Vuetify 3.7 的 `.v-alert` 自带：

```css
.v-alert { display: grid; flex: 1 1; overflow: hidden; }
.v-alert__content { align-self: center; overflow: hidden; }
```

在 `d-flex flex-column` + 固定 `height` 的容器中，`VAlert` 会被按 flex 份额压到很矮，文本被裁切。`flex-shrink-0` 只覆盖 `flex-shrink`，无法覆盖 `flex-basis: 0%`。

强制给容器内的告警加内联样式：

```python
{
    "component": "VAlert",
    "props": {
        "type": "info",
        "variant": "tonal",
        "class": "mb-3",
        "style": "flex: 0 0 auto;",
        "text": "共 N 条记录 ...",
    },
}
```

同一规则适用于其他自带 `flex` 简写的 Vuetify 组件放入固定高度 flex 容器的场景。

### 3.4 版本同步

- `plugin_version`（插件类属性）与 `package.v3.json` 的 `version` 必须一致。
- 每次用户可见修复都应在 `package.v3.json` 的 `history` 增加对应条目。

## 4. Validation & Error Matrix

| 条件 | 宿主行为 | 修复 |
| --- | --- | --- |
| 响应缺少 `data` | data client 判定非法 envelope，reject，不刷新页面，弹技术错误 toast | 补 `"data": None` |
| 响应多出额外顶层键 | 同上（`keys.length !== 3`） | 只保留三键 |
| `success: false` | toast 错误消息，不刷新（预期） | 确认 `message` 可读 |
| 按钮 API 抛异常 | FastAPI 500，前端 toast | 端点内部捕获并返回三键 envelope |
| 固定高度 flex 列中的 VAlert | 高度被压扁，文本裁切 | `style: "flex: 0 0 auto;"` |

## 5. Good / Base / Bad Cases

- Good：分页接口返回 `{"success": True, "message": "第 3 页", "data": {"page": 3}}`，前端自动重载并显示第 3 页。
- Base：扫描接口返回 `{"success": True, "message": "扫描已触发", "data": None}`，点击后页面刷新一次。
- Bad：返回 `{"success": True, "message": "第 3 页"}` —— 服务端页码变了，前端仍显示旧数据，表现为"点击下一页没反应"。

## 6. Tests Required

本仓库无法直接运行宿主，最低要求：

1. AST/静态检查：扫描 `get_api()` 声明的所有页面端点，断言每个 `return` 字典字面量的键集合恰为 `{"success", "message", "data"}`。
2. `python -m py_compile plugins.v3/<plugin>/__init__.py`。
3. `json.load(package.v3.json)` 通过，版本与 history 一致。
4. 回归（宿主环境）：打开详情页确认告警完整显示；点击分页/操作按钮确认页面自动刷新。

## 7. Wrong vs Correct

### Wrong

```python
def api_records_page(self, payload: dict = Body(...)) -> Dict[str, Any]:
    self._page = int(payload.get("page") or 1)
    return {"success": True, "message": f"第 {self._page} 页"}  # 缺 data
```

```python
{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-3 flex-shrink-0", "text": "..."}}
```

### Correct

```python
def api_records_page(self, payload: dict = Body(...)) -> Dict[str, Any]:
    self._page = int(payload.get("page") or 1)
    return {"success": True, "message": f"第 {self._page} 页", "data": {"page": self._page}}
```

```python
{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-3", "style": "flex: 0 0 auto;", "text": "..."}}
```

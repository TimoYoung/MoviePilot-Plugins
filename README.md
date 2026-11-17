# MoviePilot-Plugins（TimoYoung 第三方市场）

本仓库是个人维护的 **MoviePilot 第三方插件市场**，不是官方全量插件镜像。

## 收录插件

| 插件 ID | 目录 | 版本 | 说明 |
| --- | --- | --- | --- |
| AutoSubv2 | `plugins.v2/autosubv2` | 2.2 | V2 归档；AI 字幕生成（Whisper + 大模型翻译） |
| SmartClean | `plugins.v3/smartclean` | 1.3.8 | V3；按媒体库规则清理 Emby / Jellyfin |

索引文件：

- [`package.v2.json`](package.v2.json) — AutoSubv2
- [`package.v3.json`](package.v3.json) — SmartClean

## 在 MoviePilot 中使用

将本仓库加入插件市场（可与官方市场并存）：

```text
PLUGIN_MARKET=https://github.com/jxxghp/MoviePilot-Plugins,https://github.com/TimoYoung/MoviePilot-Plugins
```

本地开发热重载可配置：

```text
PLUGIN_LOCAL_REPO_PATHS=/path/to/this/repo
```

生产环境可将本仓库同步到挂载目录（例如 `/local-MoviePilot-Plugins`），并设置相同的 `PLUGIN_LOCAL_REPO_PATHS`。

## 说明

- AutoSubv2 保持 V2 归档形态；MoviePilot V3 可通过 V2 回退机制发现（未声明 `v3: false`）。
- SmartClean 仅提供 V3 实现；安装走文件列表（未声明 `release: true`）。
- 官方插件请继续从 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins) 安装。

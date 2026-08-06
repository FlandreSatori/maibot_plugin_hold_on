# 稍，稍等一下！（maibot_plugin.hold_on）

![](https://count.getloli.com/@FlandreSatori-hold-on?name=FlandreSatori-hold-on&theme=booru-jaypee&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

统计各模型出错率与报错原因；当某一模型 / 厂商 / 功能下的某类错误在窗口内达到阈值时，在入站 LATE 阶段 `abort`（变相插件端错误 RPM）。

## 安装

将本目录放到 MaiBot 的 `plugins/` 下，重启 MaiBot，在 WebUI 启用并配置规则。

插件 ID：`maibot_plugin.hold_on`

`auto_detect_models = true` 时，每次加载用 `tomllib` 读宿主 `config/model_config.toml`，同步 `catalog`（模型↔厂商、功能→模型）。不使用 `llm.get_available_models`（仅任务名，不够用）。

## 行为

1. **统计**：监听失败快照记错误；`replyer` 成功响应记成功。`/稍等` 按窗口展示出错率、错误类型与近期原因。
2. **停模**：匹配 `[[rules.items]]` 后写入停模到期时间；`chat.receive.after_process`（LATE）对非 `/` 命令入站 `abort`。
3. **解除**：`/解除` 只清停模，保留统计。

错误类型归类示例：`429`、`401`、`403`、`502`/`503`/`504`、`5xx`、`timeout`、`connection`、`other`。规则里可用 `*` / `any` 匹配全部，或 `5xx` / `4xx` 通配。

## 配置

| 段 | 说明 |
|---|---|
| `[plugin].auto_detect_models` | 加载时同步 catalog |
| `[stats].window_seconds` | `/稍等` 统计窗口（默认 600） |
| `[[catalog.models]]` | 模型 ↔ 厂商 |
| `[[catalog.features]]` | 功能 → 模型列表（`scope=feature` 用） |
| `[[rules.items]]` | 阈值规则 |
| `[error_watch]` | 失败快照监听 |
| `[notify]` | 新进入停模时转发通知 |
| `[permission].whitelist` | `/稍等` `/解除` 白名单 |

### 规则字段

| 字段 | 说明 |
|---|---|
| `scope` | `model` / `provider` / `feature` |
| `name` | 目标名；空 = 按实际命中目标各自计数 |
| `error_type` | 如 `429`、`5xx`、`*` |
| `window_seconds` | 滑动窗口 |
| `threshold` | 窗口内次数上限（达到即停模） |
| `hold_seconds` | 停模持续秒数 |

### 配置模板

```toml
[plugin]
enabled = true
config_version = "2.0.0"
auto_detect_models = true

[stats]
window_seconds = 600

[[rules.items]]
scope = "model"
name = ""
error_type = "429"
window_seconds = 60
threshold = 5
hold_seconds = 90

[notify]
enabled = false
target_type = "group"
group_id = ""
```

## 命令

| 命令 | 说明 |
|------|------|
| `/稍等` | 停模状态 + 各模型出错率 / 类型 / 近期原因 |
| `/解除` | 解除停模（统计保留） |

> **权限**：仅 `permission.whitelist` 或 MaiBot 全局管理员。

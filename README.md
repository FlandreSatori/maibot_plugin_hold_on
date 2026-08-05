# 稍，稍等一下！（maibot_plugin.hold_on）

![](https://count.getloli.com/@FlandreSatori-hold-on?name=FlandreSatori-hold-on&theme=booru-jaypee&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

主要生效在入站消息的 LATE 阶段；加载时可自动同步宿主启用模型。

## 安装

将本目录放到 MaiBot 的 `plugins/` 下（或你的插件目录），重启 MaiBot，在 WebUI 启用插件并填写配置。

插件 ID：`maibot_plugin.hold_on`

`auto_detect_models = true` 时，**每次加载**都会读取宿主 `config/model_config.toml`，同步 `limits` 与 `feature_kill.features`。同名模型/厂商会保留你已配置的 RPM / 禁用秒数。

## 配置

| 段 | 说明 |
|---|---|
| `[plugin].auto_detect_models` | 加载时自动检测并同步启用模型 |
| `[plugin].model_config_path` | 可选：`model_config.toml` 绝对路径 |
| `[global_limit].enabled` | 限流/禁用总开关 |
| `[global_limit].max_requests_per_minute` | （已弃用）保持 `0`（全局 RPM 无意义，改用厂商/模型 RPM） |
| `[global_limit].disable_seconds` | 功能全灭等「全局停模」的禁用秒数（默认 90） |
| `[[limits.providers]]` | 厂商 RPM（默认 **30**/分钟，超限禁用 **90** 秒） |
| `[[limits.models]]` | 模型 RPM（默认 **10**/分钟，超限禁用 **90** 秒） |
| `[error_disable].base_seconds` | 首次模型错误禁用秒数 |
| `[error_disable].exponential` | 连续错误是否指数退避 |
| `[feature_kill].enabled` | 某项功能下的模型全部遇到错误时是否全局禁用LLM |
| `[[feature_kill.features]]` | 功能名 → 模型列表 |
| `[error_watch].roots` | 额外监听的失败快照目录 |
| `[notify].enabled` | 触发 RPM / 新全局停模时是否转发通知 |
| `[notify].target_type` | `group` / `private` / `stream_id` |
| `[notify].group_id` / `user_id` / `stream_id` | 对应目标 |
| `[notify].prefix` | 通知前缀，默认 `[hold_on] ` |
| `[permission].whitelist` | 管理命令白名单 |

### 配置模板示例

```toml
[plugin]
enabled = true
config_version = "1.3.1"
auto_detect_models = true
model_config_path = ""

[global_limit]
enabled = true
# 全局 RPM 已停用，保持 0
max_requests_per_minute = 0
disable_seconds = 90

# 自动同步时新项默认：厂商 30 RPM / 模型 10 RPM，超限禁用 90 秒
# [[limits.providers]]
# name = "openai"
# max_requests_per_minute = 30
# disable_seconds = 90
#
# [[limits.models]]
# name = "replyer"
# provider = "openai"
# max_requests_per_minute = 10
# disable_seconds = 90

[error_disable]
enabled = true
base_seconds = 60
max_seconds = 3600
exponential = true
multiplier = 2.0

[feature_kill]
enabled = true

[error_watch]
enabled = true
interval_seconds = 2.0

[notify]
enabled = false
target_type = "group"
group_id = ""
user_id = ""
stream_id = ""
platform = "qq"
prefix = "[hold_on] "

[permission]
whitelist = []
notify_permission_denied = true
```

限流生效顺序：**厂商 RPM → 模型 RPM**（全局 RPM 已关闭）。两者同时生效，更严的先拦住。

## 通知转发

开启 `[notify].enabled` 后，在以下**新触发**时向目标会话发一条文本（同批合并为一条，避免刷屏）：

- 厂商 / 模型 RPM 超限
- 新进入全局停模（含功能模型全灭）

目标解析方式与 `redirect_err` 相同：`group` / `private` / `stream_id`。

## 命令

| 命令 | 说明 |
|------|------|
| `/稍等` | 查看当前禁用与全局停模状态 |
| `/解除` | 全局解除全部禁用 |

> **权限**：仅 `permission.whitelist` 或 MaiBot 全局管理员可执行。白名单支持 `user_id` 或 `platform:user_id`。

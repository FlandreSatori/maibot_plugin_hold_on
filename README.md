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
| `[global_limit].max_requests_per_minute` | 全局每分钟请求上限（`0`=不限） |
| `[global_limit].disable_seconds` | 全局超限后禁用秒数 |
| `[[limits.providers]]` / `[[limits.models]]` | 厂商 / 模型 RPM 与禁用秒数 |
| `[error_disable].base_seconds` | 首次模型错误禁用秒数 |
| `[error_disable].exponential` | 连续错误是否指数退避 |
| `[feature_kill].enabled` | 某项功能下的模型全部遇到错误时是否全局禁用LLM |
| `[[feature_kill.features]]` | 功能名 → 模型列表 |
| `[error_watch].roots` | 额外监听的失败快照目录 |
| `[permission].whitelist` | 管理命令白名单 |

### 配置模板示例

```toml
[plugin]
enabled = true
config_version = "1.2.1"
auto_detect_models = true
model_config_path = ""

[global_limit]
enabled = true
max_requests_per_minute = 60
disable_seconds = 300

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

[permission]
whitelist = []
notify_permission_denied = true
```

## 命令

| 命令 | 说明 |
|------|------|
| `/稍等` | 查看当前禁用与全局停模状态 |
| `/解除` | 全局解除全部禁用 |

> **权限**：仅 `permission.whitelist` 或 MaiBot 全局管理员可执行。白名单支持 `user_id` 或 `platform:user_id`。

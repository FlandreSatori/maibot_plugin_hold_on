# 稍，稍等一下！

![](https://count.getloli.com/@FlandreSatori-hold-on?name=FlandreSatori-hold-on&theme=booru-jaypee&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

插件通过 `llm_usage` 统计成功调用、Token 与成本，通过 `llm_error` 错误快照（兼容 schema v3/v6）统计失败；达到静态速率或动态预算条件后，在 LATE 阶段停止新的入站消息。

## 配置含义

固定选项在 WebUI 中使用下拉框。模型、厂商和功能名称可由宿主配置自动探测，也可在“模型列表”中手动补充。动态预算启用时，不会叠加静态限制。

### 插件

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 插件总开关；关闭后不执行限速、错误监听或转发图片拦截。 |
| `config_version` | `3.0.0` | 插件配置版本标记。 |
| `auto_detect_models` | `true` | 启动或配置更新时自动读取宿主的模型、厂商和功能映射。 |
| `model_config_path` | `""` | 宿主 `model_config.toml` 的额外路径；留空则自动查找。 |
| `forward_image_threshold` | `0` | 合并转发消息内的图片数达到该值时直接停止入站；`0` 为关闭。 |

### 统计

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `window_seconds` | `120` | 限速检查间隔和状态命令使用的统计窗口，单位为秒。插件启动或配置更新后先等待一个完整间隔，之后由间隔到期后的首条入站消息触发检查；动态预算的累计周期不受此值改变。 |
| `usage_limit` | `5000` | 每次聚合成功调用时最多读取的记录数。 |

### 模型列表

`catalog.models` 用于手动补充模型映射，每项包含 `name`（模型别名）和 `provider`（厂商名）。`catalog.features` 用于手动补充功能映射，每项包含 `feature`（对应 `task_name`）和 `models`（该功能使用的模型别名列表）。自动探测结果优先，手动配置会补充未探测到的条目。

### 静态限制

`static_limits.enabled` 默认 `false`，仅在动态预算未启用或没有预算规则时生效。每个 `static_limits.items` 规则有以下配置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `scope` | `model` | 限制范围：`provider`、`model` 或 `feature`。 |
| `target` | `""` | 限制目标；留空时分别统计该范围下每个实际目标。 |
| `metric` | `requests` | 限制指标：请求数 `requests`、加权 Token `tokens` 或成本 `cost`。 |
| `window_seconds` | `60` | 滑动窗口秒数。 |
| `limit` | `10` | 窗口内允许的最大指标值。 |
| `input_weight` | `1.0` | 计算 Token 时输入 Token 的倍率。 |
| `output_weight` | `1.0` | 计算 Token 时输出 Token 的倍率。 |

### 动态预算

`budget.enabled` 默认 `true`。启用后仅使用 `budget.items`，按当前消耗和剩余时长控制速度，尽量在周期内均匀用完额度。预算检查按 `stats.window_seconds` 节流，不会对每条消息重复查询 `ModelUsage`；每个预算规则包含：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `scope` | `model` | 预算范围：`provider`、`model` 或 `feature`。 |
| `target` | `""` | 预算目标；留空时按该范围下的每个实际目标分别计算。 |
| `metric` | `cost` | 预算指标：加权 Token `tokens` 或成本 `cost`。 |
| `amount` | `100` | 单个周期允许使用的总额度。 |
| `start_time` | `08:00` | 每日周期开始时间，格式为 `HH:MM`。 |
| `end_time` | `22:00` | 每日周期结束时间，格式为 `HH:MM`；早于或等于开始时间表示跨天周期。 |
| `strategy` | `strict` | `strict` 超过计划速度立即停止；`balanced` 允许短暂超速。 |
| `overshoot_time` | `300` | `balanced` 策略允许领先计划曲线的秒数。 |
| `off_hours` | `continue` | 预算时段外行为：`hold` 停止至下一周期开始；`continue` 不按速度限流，但总额耗尽后仍停止。 |
| `input_weight` | `1.0` | 计算 Token 时输入 Token 的倍率。 |
| `output_weight` | `1.0` | 计算 Token 时输出 Token 的倍率。 |

触发静态限制或预算限制时，会按“通知”配置发送通知。

### 错误阈值

`error_rules.enabled` 默认 `true`。错误在滑动窗口内达到阈值时停止入站，连续触发且期间没有同一目标成功调用时，停止时长会按 `1x`、`2x`、`3x` 线性增长，直到上限。每个 `error_rules.items` 规则包含：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `scope` | `feature` | 计数范围：`provider`、`model` 或 `feature`。 |
| `name` | `""` | 目标名；留空时按实际命中的目标分别计数。 |
| `error_type` | `*` | 错误类型：`429`、`5xx`、`timeout` 或 `*`（全部错误）。 |
| `window_seconds` | `120` | 错误滑动窗口秒数。 |
| `threshold` | `5` | 窗口内达到该次数时停止入站。 |
| `hold_seconds` | `90` | 首次触发的停止秒数。 |
| `hold_max_seconds` | `3600` | 连续触发后停止时长的上限。 |

### 错误统计

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否监听宿主 `logs/maisaka_prompt/llm_error` 下的错误快照。 |
| `interval_seconds` | `2.0` | 扫描间隔，单位为秒，最小为 `0.5`。 |
| `roots` | `[]` | 额外扫描的错误快照目录或单个 JSON 文件路径。默认目录与 schema v3/v6 均兼容。 |

### 通知

`notify.enabled` 默认 `false`。开启后，错误阈值、静态限制和预算限制触发的停止都会发送文本通知。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `target_type` | `group` | 通知目标类型：`group`、`private` 或 `stream_id`。 |
| `group_id` | `""` | 当 `target_type=group` 时使用的群号。 |
| `user_id` | `""` | 当 `target_type=private` 时使用的用户 ID。 |
| `stream_id` | `""` | 当 `target_type=stream_id` 时直接使用的聊天流 ID。 |
| `platform` | `qq` | 群聊或私聊目标所属的平台。 |
| `prefix` | `[hold_on]` | 每条通知正文前添加的前缀。 |

### 权限

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `whitelist` | `[]` | 可使用管理命令的用户 ID 列表；支持 `user_id` 或 `platform:user_id`。 |
| `notify_permission_denied` | `true` | 无权限使用管理命令时是否返回“权限不足”提示。 |


## 命令

- `/稍等`：显示当前窗口成功/失败/限速、token（M）、成本（¥/小时），以及监听目标最近一次成功与错误详情。
- `/解除`：解除当前停止状态，保留统计。


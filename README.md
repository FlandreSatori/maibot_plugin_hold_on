# 稍，稍等一下！

![](https://count.getloli.com/@FlandreSatori-hold-on?name=FlandreSatori-hold-on&theme=booru-jaypee&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

插件通过 `llm_usage` 统计成功调用、Token 与成本，通过 `llm_error` schema v3 统计失败；达到静态速率或动态预算条件后，在 LATE 阶段停止新的入站消息。

## 配置

固定选项在 WebUI 中使用下拉框；模型、厂商和功能名称由宿主配置自动探测。配置字段说明以 WebUI 为准

- `static_limits`：动态预算关闭时，按厂商、模型或功能限制请求数、加权 Token 或成本。
- `budget`：启用后只使用动态预算。预算按每日时间段均匀消耗。
- `strict`：按计划曲线超速立即停止。
- `balanced`：允许有限透支，透支后等待追回。
- `lenient`：允许更大的自定义透支比例。
- Token 指标使用 `input_tokens * input_weight + output_tokens * output_weight`。

## 命令

- `/稍等`：显示当前窗口的成功调用、Token、成本、错误与限制状态。
- `/解除`：解除当前停止状态，保留统计。

## 数据语义

成功数据通过宿主已有的 `database.query` 读取 `ModelUsage`（表 `llm_usage`），插件按时间窗自行过滤并聚合；失败来自 `logs/maisaka_prompt/llm_error` 的 schema v3 attempt。不依赖宿主新增统计 capability。

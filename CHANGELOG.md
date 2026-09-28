# 更新日志

## 未发布

- 动态预算和静态限速改为按 `stats.window_seconds` 节流检查，减少每条消息查询 `ModelUsage` 的开销。

## 3.3.0

- 默认限速检查间隔调整为 120 秒。
- 插件启动、配置更新或手动解除后先等待一个完整检查间隔，不再由首条消息立即触发检查。

## 3.2.4

- 动态预算和静态限速改为按 `stats.window_seconds` 节流检查，减少每条消息查询 `ModelUsage` 的开销。
- 首条普通消息立即检查，后续检查在窗口到期后执行；限速 hold 期间直接拦截，不重复查询数据库。

## 3.2.3

- 修复动态预算从 `00:00` 开始时的周期边界判断，避免午夜预算被错误地立即判定为超速。
- 预算统计改用半开时间窗，周期结束时刻的用量不会重复计入下一周期。
- 修复手动 `/解除` 后同一预算周期内立即再次触发限速的问题。

## 3.2.2

- 新增 `stats.include_failed_requests`，默认将 `ModelUsage` 中明确标记失败的请求纳入请求数、Token 和成本统计，以尽量对齐 API 账单。
- 修复 MaiBot schema v6 失败请求快照的错误统计：兼容 `generation_attempts`、新版模型与厂商字段，以及请求中的功能名。

## 3.2.1

- 优化统计文本

## 3.0.1

- 触发限速（含预算超速、时段外停止）时，与错误阈值一样转发通知。
- `/稍等` 增加限速触发次数统计（窗口合计与分目标）。
- 动态预算新增 `off_hours`：`hold` 在指定时段外停止响应；`continue` 时段外不控速，继续花完剩余额度。

## 3.0.0

- 重构为基于 llm_usage 的成功调用、Token、成本监控。
- 成功数据改为宿主 `database.query(ModelUsage)` + 插件内时间窗聚合（兼容 OneKey，不依赖新增 capability）。
- 支持 provider/model/feature 静态限制与每日动态预算。
- 支持 strict、balanced 预算策略；balanced 用 overshoot_time 控制可超前秒数。
- 恢复错误阈值停模：窗口内达限后 LATE abort，停止时长线性增长。
- 错误仅解析 MaiBot schema v3 的 llm_error 快照。

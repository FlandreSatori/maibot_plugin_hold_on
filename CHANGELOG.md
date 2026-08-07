# 更新日志

## 3.0.0

- 重构为基于 llm_usage 的成功调用、Token、成本监控。
- 成功数据改为宿主 `database.query(ModelUsage)` + 插件内时间窗聚合（兼容 OneKey，不依赖新增 capability）。
- 支持 provider/model/feature 静态限制与每日动态预算。
- 支持 strict、balanced 预算策略；balanced 用 overshoot_time 控制可超前秒数。
- 恢复错误阈值停模：窗口内达限后 LATE abort，停止时长线性增长。
- 错误仅解析 MaiBot schema v3 的 llm_error 快照。

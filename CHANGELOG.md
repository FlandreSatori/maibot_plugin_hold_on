# 更新日志

## 2.0.2

- 清理遗留兜底：去掉本地 success 计数、旧快照顶层 error、`llm_request` 监听、v1 配置合并等。
- 成功只读 `llm_usage`；失败只解析 `llm_error` schema v3 的 `attempts[].error`。

## 2.0.1

- 成功次数改为读取宿主 `llm_usage`（`ModelUsage`），与 `maibot_statistic` 的「调用次数 / REQ_CNT_BY_MODEL」同源；不再用 replyer hook 估成功。
- manifest 增加 `database.query`。

## 2.0.0

- **功能重定义**：去掉请求 RPM / 单模型禁用 / 功能全灭逻辑。
- 统计各模型成功/失败与报错类型、近期原因；`/稍等` 查看。
- 新增 `[[rules.items]]`：某一 `model` / `provider` / `feature` 下某类错误在窗口内达阈值 → LATE `abort` 停模。
- `/解除` 仅解除停模，保留统计。
- 配置结构破坏性变更（`global_limit` / `limits` / `error_disable` / `feature_kill` 移除；改用 `stats` / `catalog` / `rules`）。

## 1.3.2

- 从 manifest 移除未使用的 `llm.get_available_models`。
- 修复通知：模型错误新进入禁用时也会转发。

## 1.3.1

- 停用全局 RPM；厂商 / 模型默认 RPM 30 / 10，禁用 90 秒。

## 1.3.0

- 新增 `[notify]` 转发。

## 1.2.1

- `auto_detect_models` 每次加载同步；移除 `models_auto_filled`。

## 1.2.0

- 命令精简为 `/稍等`、`/解除`；全局停模仅 LATE abort。

## 1.1.0

- 自动从宿主 `model_config.toml` 填入模型 / 厂商 / 功能。

## 1.0.0

- 初版。

# 更新日志

## 1.2.1

- `auto_detect_models` 改为每次加载时同步宿主启用模型（不再仅首次）；同名项保留已配置的 RPM / 禁用秒数。
- 移除 `models_auto_filled` 配置项。

## 1.2.0

- 命令精简为 `/稍等`、`/解除`。
- 全局停模改为仅 `chat.receive.after_process` abort 入站；放行 `/` 命令；不再改写 planner / replyer，不再调 frequency。

## 1.1.0

- 首次运行自动从宿主 `model_config.toml` 填入模型 / 厂商 / 功能列表。
- 精简错误禁用：失败快照命中模型即禁用，不再按错误类型匹配。

## 1.0.0

- 初版（纯插件，不改 MaiBot 宿主）。

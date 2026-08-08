# 稍，稍等一下！

![](https://count.getloli.com/@FlandreSatori-hold-on?name=FlandreSatori-hold-on&theme=booru-jaypee&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

插件通过 `llm_usage` 统计成功调用、Token 与成本，通过 `llm_error` schema v3 统计失败；达到静态速率或动态预算条件后，在 LATE 阶段停止新的入站消息。

## 配置含义

固定选项在 WebUI 中使用下拉框；模型、厂商和功能名称由宿主配置自动探测。配置字段说明以 WebUI 为准

- 插件
  - auto_detect_models：加载插件时是否自动读取模型列表
  - model_config_path：留空表示读取默认位置

- 统计
  - windows_seconds：统计窗口
  - usage_limit：单词最多读取成功调用数量

- 模型列表
  - 插件启动时会自动读取已配置的模型(model)和功能(feature)

- 静态限制
  - 对厂商（provider）/ 模型（model）/ 功能 （feature）提供静态速率限制
  - 可以限制 请求速度（requests） /  额度（tokens） / 成本 （cost）

- 动态预算
  - 可以动态地控制使用速度，尽量在指定的时间范围内均匀地用完额度
  - 预估速度=剩余额度/剩余时间
  - strict：超出预估速度则马上停止，下次统计时按追上计划曲线所需时间续停
  - balanced：允许超速 overshoot_time 秒，再按追上曲线所需时间停止
  - off_hours：时段外 `hold` 停止响应，或 `continue` 不控速继续花完剩余额度
  - 触发限速时会按通知配置转发（与错误阈值相同）

- 错误阈值
  - 当某一项功能出错次数到达指定值时，停止LLM响应避免产生无意义的消耗
  - 同时转发到指定群聊
  - 停止时间会线性增长

- 权限
  - 可以使用命令的白名单


## 命令

- `/稍等`：显示当前窗口成功/失败/限速、token（M）、成本（¥/小时），以及监听目标最近一次成功与错误详情。
- `/解除`：解除当前停止状态，保留统计。


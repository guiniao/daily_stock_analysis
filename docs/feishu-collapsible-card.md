# 飞书折叠卡片（Fork 自定义扩展）

将原本一条超长的飞书文本消息，改为**一张交互卡片**：顶部总览 + 每只股票一个折叠面板（默认收起，点击展开看明细）。解决多股票刷屏、阅读友好度差的问题。

## 效果

群里只收到一张卡片（股票过多时自动分多张）：

- **顶部总览**（永久显示）：股票数量、买入/观望/卖出统计、大盘状态行、评分速览列表。
- **每只股票一个 `collapsible_panel`**：标题行 = `🟢 紫金矿业(601899) · 买入 · 评分 72`，默认折叠。点击展开看该股完整明细（舆情/基本面、核心结论、数据透视、作战计划、信号归因等，与原 markdown 报告字段一致）。再点收起。
- 各面板**互相独立**，可同时展开多只，互不影响。

> 与"每股一张卡"的区别：折叠面板把所有股票收在**一张卡**里，群里不刷屏；想看哪只点开哪只。

## 新消息分隔条

每次推送会**先发一条独立的分隔卡**，再发折叠分析卡。群里看到的是：

```
[上一次推送的消息]
   ↕ 飞书自然的消息间隔
🆕 新消息 · 2026-07-22 22:30   ← 红色 header + 横线的分隔条（独立一条消息）
   ↕ 飞书自然的消息间隔
📊 折叠分析卡（总览 + 各股票面板）
```

分隔条把本次推送和上一次推送在视觉上隔开，避免历史消息和新消息混在一起无法区分。带推送时间戳。

> 说明：飞书 webhook 无法控制消息之间的物理间距（间距由客户端决定），故用"独立分隔条消息"实现视觉分隔，靠飞书自然的消息间隔拉开上下。

## 启用方法（GitHub Secrets）

在 Fork 仓库 **Settings -> Secrets and variables -> Actions** 添加：

| Secret | 值 | 说明 |
|---|---|---|
| `FEISHU_COLLAPSIBLE_CARD` | `true` | 开启折叠卡片。不设或设其他值则保持原文本推送。 |
| `FEISHU_COLLAPSIBLE_MAX_ITEMS` | `15`（可选） | 单张卡最多容纳的股票面板数。超出自动分多张卡（飞书单卡 JSON 上限约 30KB）。默认 15。 |

前提：飞书走 **Webhook 模式**（`FEISHU_WEBHOOK_URL` 已配置）。本功能仅适配 Webhook 路径；App Bot 模式不受影响、走原逻辑。**不改动 config.py**，开关走环境变量。

## 已知问题与修复（2026-08-06）

**问题：相同内容被重复推送。** 根因是折叠分支只看 `_last_feishu_results` 是否存在、不看本次要发的内容，
导致大盘复盘、飞书文档链接等**非股票报告**在发到飞书时也被折叠卡片拦截，把同一份股票卡片又重发一遍。

**修复：** 只有本次发送的内容正是最近一次聚合报告本身（`send_to_feishu(content)` 的 `content == 暂存报告`）时，
才走折叠卡片；其它内容一律走普通 webhook 路径。这样大盘复盘等内容能正常送达，股票卡片不再重复。

## 关闭 / 回退

- 删除或置空 `FEISHU_COLLAPSIBLE_CARD` Secret -> 立即回退原文本推送，无需改代码、无需回滚仓库。
- 折叠卡片构建异常时，代码自动回退纯文本摘要推送，报告不丢。

## 技术要点（实测确认）

飞书自定义机器人 webhook 对卡片结构有严格要求，以下是实测确认的可用结构：

- **必须用 `schema: "2.0"` + `body.elements`**（顶层 `elements` 会被拒，解析报错）。
- 折叠组件 `collapsible_panel`：字段为 `header.title` + `elements`，可选 `expanded`(默认 false)、`border`。
- 支持的基础元素：`div`(lark_md)、`hr`、`note`、`action`+`button`、`column_set`。
- **不支持**：`collapsible_group`、`divider`、`background_style`、顶层 `elements`、`accordion`/`fold` 等其他折叠组件名。
- 客户端版本不能过低，老版飞书可能不渲染折叠面板。

## 文件清单

| 文件 | 类型 | 作用 | 冲突风险 |
|---|---|---|---|
| `src/notification_sender/feishu_collapsible.py` | 新增 | 折叠卡片 JSON 构建（复用 `NotificationService` 的渲染 helper，明细与 markdown 报告一致）。 | ❌ 几乎不冲突 |
| `src/notification_sender/feishu_sender.py` | 小改 | 入口分支：开关开启且有 results 时走折叠卡片；新增 `_post_webhook_payload` / `_send_collapsible_cards`。 | ⚠️ 低（唯一分支点） |
| `src/notification.py` | 1 行 | `generate_aggregate_report` 暂存 `_last_feishu_results` 供发送器读取。 | ⚠️ 低 |

未改动 `config.py`、`pipeline.py` 等大文件，开关走环境变量，便于上游合并。

## 持续跟进上游更新

配套 `.github/workflows/sync-upstream.yml` 自动把上游 `main` 合并进本 Fork 的 `custom` 分支。折叠卡片改动集中在 `feishu_collapsible.py`（新文件，上游不会触碰）+ `feishu_sender.py`/`notification.py` 的小分支点，正常合并自动通过；仅当上游重构那两处入口时可能产生几行冲突，手动解决即可。

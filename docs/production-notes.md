# 长期运行记录

[返回 README](../README.md)

本文汇总 Obsidian Vow 生产实例的脱敏统计。它用来说明项目的数据谱系、迁移过程和真实运行边界，不公开任何对话正文或个人生活数据。

所有数字来自 2026 年 9 月 1 日的一次部署环境只读检查。生产数据库当时通过 SQLite `quick_check`。

## 数据谱系

| 时间 | 记录 |
| --- | --- |
| 2026 年 3 月 18 日 | 当前数据线中最早的 conversation 和 message |
| 2026 年 4 月 17 日 | 连续聊天日归档的起点 |
| 2026 年 5 月 11 日 | Obsidian Vow 重构开始 |
| 2026 年 8 月 3 日 | Memory V3 迁移前完整备份 |
| 2026 年 9 月 1 日 | 本文使用的只读快照 |

从 2026 年 4 月 17 日到 9 月 1 日，共保存 138 份逐日聊天归档，日期连续。它们只能证明每天都有持久化记录，不能当作进程 uptime 或零停机证明。

项目最初运行在 AionsHome 上。Obsidian Vow 沿用原数据逐步改写，没有为了迁移 Memory V3 而清空对话、旧记忆或来源关系。

## 2026 年 9 月快照

| 类别 | 数量 |
| --- | ---: |
| `conversation` | 139 |
| `message` | 22,207 |
| 来源 `chunk` | 14,441 |
| Legacy memory | 542 |
| Memory V2 item | 6,053 |
| 保留 legacy linkage 的 Memory V2 item | 182 |
| Relational Card | 556 |
| Timeline version | 851 |
| Working Model version | 61 |
| Desire version | 26 |
| Vow | 6 |
| Memory injection event | 11,278 |
| Tool invocation event | 11,922 |
| Self-Wake | 48 |

556 张 Relational Card 中，513 张处于 active 状态，37 张已 superseded，6 张 invalid。

11,278 次记忆注入由两部分组成：普通召回 5,941 次，Timeline 5,337 次。这里统计的是实际进入上下文的审计事件，不是候选检索次数。

## 一次原地迁移

2026 年 8 月 3 日的 Memory V3 上线前备份包含 17,696 条 message 和 10,661 个 chunk。当时 Timeline、Relational Card、Working Model、Desire 和 Vows 等新表均为空。

后续快照继续沿用同一份数据。新表是在已有对话和记忆上迁移、生成并继续增长的。旧 `memories` 表也仍然保留，新旧来源可以区分。

前后快照至少能确认，Memory V3 和三层关系状态确实进入了长期使用中的实例。它们不能证明这些机制提升了关系质量，也不能替代召回或生成评测。

## 功能状态与证据边界

| 机制 | 快照中可见的证据 | 还不能得出的结论 |
| --- | --- | --- |
| Working Model、Desire、Vows | 有版本记录，并进入多个 Core turn 的稳定上下文 | 长期更新是否避免了漂移和固化 |
| Timeline | 851 个版本，5,337 次注入 | 三日常驻上下文是否优于其他窗口 |
| Relational Cards | 556 张卡片，含状态与来源 | 是否改善了关系相关召回或最终回答 |
| Pending Recall | 功能已实现并启用，表中为 0 条 | 真实使用中的成本、延迟和召回收益 |
| Reflection | 功能已实现并启用，日志表中为 0 条 | 反思是否会带来更可靠的认识更新 |
| 跨设备上下文 | Android、Windows、位置和日程证据管道已运行 | 最小信号集合与隐私—效用曲线 |

## 一次 Harness 故障

一个只应在 BLE 控制会话内成立的能力曾泄漏进持久化全局配置。会话失效后，自治轮次仍向 Core 宣告设备可用，执行网关则依据实时会话拒绝动作。

事故窗口内共有 20 次 Core wake，其中 13 次生成设备命令，13 次全部以 `no_active_session` 被拒绝。没有命令越过执行网关，但 prompt 中的能力状态已经过期。

修复删除了重复的布尔权限源。Prompt 构造与执行网关现在都读取活跃会话和实时设备服务；执行前还会重新核对 session id、control epoch、owner 和在线状态。

这次故障说明，模型看到的 capability snapshot 应与负责执行的服务来自同一套状态。

## 公开边界

仓库和本文不包含以下内容：

- 对话、AI Notes、Working Model、Desire、Vows 和 Timeline 的正文；
- 逐条消息时间戳、位置历史、健康记录和设备活动；
- 生产数据库、备份、日志原文、设备标识和密钥；
- 能从聚合结果反推出具体生活事件的细分统计。

公开仓库保留数据结构、实现代码和合成测试。生产统计只提供聚合后的数量、时间范围和解释边界。

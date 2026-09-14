# Agent Collab 消息协议与通知格式

> 记录两条已确认的使用规则。适用于所有接入 agent-collab 的 Claude/Codex 会话。

## 1. 收信时间确认与批量回复

收到 peer 消息后，先核对消息 `time` 与当前时间的差距，再决定回复方式：

- **新鲜消息**（时间差在几分钟内）：按需正常回复；
- **较旧消息或积压多条**（时间差明显，或一次收到同一会话多条未答消息）：**按主题合并，一次性回复一条**，覆盖全部待答事项；不做逐条 ping-pong 式往返。

规则目的：**减少消息发送数量**。协调消息本身有成本（打断对端、注入上下文、产生新的通知行），宁可一条说全，不发碎片消息。

合并回复时仍须逐点对应（可引用 `msg_id` 前缀或逐条编号），不因合并而漏答；纯粹的状态回执（如口径确认）在对方已无后续问题时可以省略。

## 2. 通知行格式：箭头标注方向与对端

现状的排队通知行只给 Message ID，看不出方向和对端：

```text
[Agent Collab] An authenticated peer message is queued. Message ID: f4cc7d17-f0e6-43d5-bcdf-1f01ffcc76ea
```

期望格式：**用箭头写清是发送还是接收、给谁/来自谁**，消息 ID 取前 8 位即可定位（完整 ID 可在 MCP 结果与收件箱查）：

```text
发送（出站，queued 重试中）:
[Agent Collab] → codex-01a08bdb@host-a queued f4cc7d17

发送（出站，已送达）:
[Agent Collab] → codex-01a08bdb@host-a delivered f4cc7d17

接收（入站，进入本会话收件箱）:
[Agent Collab] ← codex-01a08bc5@host-a queued 118cb7e5
```

- 箭头 `→` = 本会话发出；`←` = 收到对端消息；
- 对端用会话名（`codex-01a08bdb@host-a` 形式）；
- 状态只保留 `queued` / `delivered` 两档。

> 已实现：入站 queue 通知首行、`send` 结果的出站初始行（`queued`）、发送方 sidecar 在对端取件归档后注入本会话的出站终态行（Claude 接收为 `delivered`，Codex 接收为 `queued`）均按本格式落地；终态行迟迟不来 = 对端未在收件。

房间消息的通知行在上述格式上追加房间短 ID 与对话名：

```text
入站（房间消息排队通知）:
[Agent Collab] ← glm-k7 #a7x9 queued 118cb7e5

出站初始（send 工具结果）:
[Agent Collab] → #a7x9 (2) queued f4cc7d17

出站终态（发送方聚合，多成员带计数）:
[Agent Collab] → #a7x9 delivered 2 f4cc7d17
```

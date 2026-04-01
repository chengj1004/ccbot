# 企业微信智能机器人（长连接模式）适配方案

## 背景

当前 ccbot 已支持企微 **自建应用** 模式（`ccbot wecom`），通过 HTTP 回调接收消息、通过 REST API 发送消息。现在需要新增 **智能机器人** 模式（`ccbot wecom-bot`），通过 WebSocket 长连接收发消息。

两种模式是企微完全独立的应用类型，不能混用 API。

## 两种模式对比

| 特性 | 自建应用 (现有) | 智能机器人 (新增) |
|---|---|---|
| 接收消息 | HTTP POST 回调（加密 XML） | WebSocket 长连接（JSON） |
| 发送消息 | REST API (`appchat/send`, `message/send`) | WebSocket 帧 (`aibot_respond_msg`) 或 `response_url` |
| 认证凭证 | corp_id + secret + agent_id + callback_token + encoding_aes_key | bot_id + bot_secret |
| 需要公网 | **是**（回调 URL 必须 HTTPS） | **否**（出站 WebSocket，无需公网） |
| 消息加密 | AES-CBC 加解密 + 签名验证 | WebSocket TLS 自带加密 |
| 主动推送 | 支持（REST API 随时可调） | **不支持**（只能回复用户消息） |
| 群聊类型 | 应用群聊（API 创建，≥2 人） | 普通群聊（加入机器人即可） |
| 媒体操作 | access_token + media API | 需额外配置 corp_id/secret（可选） |
| 响应形式 | 独立消息 | **流式响应**（单条消息实时更新） |

**核心优势**：智能机器人模式 **不需要公网服务器**，WebSocket 是出站连接。

**核心限制**：智能机器人模式 **不能主动推送消息**，只能在回复用户消息的上下文中发送。

## WebSocket 长连接协议

### 连接地址

```
wss://openws.work.weixin.qq.com
```

### 生命周期

```
1. 建立 WebSocket 连接
2. 发送 aibot_subscribe（认证）
3. 收到 errcode=0（认证成功）
4. 启动心跳循环（每 30s 发 ping）
5. 接收消息回调（aibot_msg_callback / aibot_event_callback）
6. 发送响应（aibot_respond_msg）
7. 连接断开 → 指数退避重连
```

### 帧格式

所有通信使用 JSON 文本帧：

**发送帧**：
```json
{
  "cmd": "aibot_subscribe | ping | aibot_respond_msg",
  "reqId": "唯一ID，用于追踪 ack",
  "body": { ... }
}
```

**接收帧**：
```json
// 消息回调
{
  "cmd": "aibot_msg_callback",
  "reqId": "xxx",
  "body": {
    "msgid": "CAIQrcjMjQYY...",
    "aibotid": "BOT_ID",
    "chatid": "CHAT_ID",
    "chattype": "group | single",
    "from": { "userid": "USER_ID", "corpid": "CORP_ID" },
    "response_url": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?response_code=...",
    "msgtype": "text | image | voice | mixed | event",
    "text": { "content": "消息内容" }
  }
}

// 事件回调
{
  "cmd": "aibot_event_callback",
  "body": {
    "msgtype": "event",
    "event": { "eventtype": "enter_chat" },
    "from": { "userid": "...", "corpid": "..." }
  }
}

// 命令 ack
{ "reqId": "xxx", "errcode": 0 }

// 心跳 ack
{ "cmd": "pong" }
```

### 心跳

- 每 30 秒发送 `{"cmd": "ping", "reqId": "..."}`
- 服务端回复 `{"cmd": "pong"}`
- 连续 2 次未收到 pong → 强制断开重连

### 重连策略

- 基础延迟：5 秒
- 最大延迟：60 秒
- 指数退避：`delay = min(5 × 2^attempts, 60)`

## 流式响应机制

### 核心概念

智能机器人的回复采用 **流式（stream）** 模式。每条用户消息对应一个 stream，在企微客户端上表现为 **一条持续更新的消息**（类似打字机效果）。

```json
// 发送流式响应
{
  "cmd": "aibot_respond_msg",
  "reqId": "unique_req_id",
  "body": {
    "msgtype": "stream",
    "stream": {
      "id": "stream_unique_id",
      "content": "Markdown 内容（累积替换）",
      "finish": false,
      "msg_item": []
    }
  }
}
```

**关键规则**：
- `stream.id`：首次回复时由 bot 生成，后续更新使用同一 id
- `content`：每次发送 **完整内容**（替换，非追加），支持 Markdown
- `content` 中的 `<think>...</think>` 标签会被企微渲染为可折叠的"思考过程"UI
- `finish: true`：标记流结束，之后不能再更新
- `msg_item`：仅在 `finish: true` 时可携带图片（base64，最多 10 张，每张最大 2MB）
- 内容大小限制：20480 字节 UTF-8

### response_url（备选通道）

每条用户消息附带一个 `response_url`：
- 有效期 1 小时
- **一次性**，POST 后即失效
- 支持发送 `text`、`markdown`、`template_card`
- 在群聊中自动引用触发消息

```
POST {response_url}
{
  "msgtype": "markdown",
  "markdown": { "content": "回复内容" }
}
```

## ccbot 适配方案

### 核心挑战

ccbot 的 Claude 响应是 **异步、多条** 的（thinking → tool_use → tool_result → text），而智能机器人模式要求 **每条用户消息对应一个 stream 回复**。

### 解决方案：Per-Chat Stream 累积

```
用户发消息 ──→ 创建 stream（显示 ⏳）──→ 转发给 Claude
                                              │
Claude 输出 text ──→ 追加到 stream content ──→ 更新 stream（finish=false）
Claude 输出 text ──→ 继续追加 ──────────────→ 更新 stream（finish=false）
                                              │
5 秒无新输出 / 用户发新消息 ──────────────────→ finish stream（finish=true）
```

**每个 chatid 维护一个 ChatStream**：

```python
@dataclass
class ChatStream:
    stream_id: str           # 本次流的唯一 ID
    content: str = ""        # 累积的内容
    finished: bool = False   # 是否已结束
```

**流的生命周期**：

1. **创建**：用户发送消息时，创建新 stream
   - 如果已有未完成的 stream → 先 finish 旧的
   - 发送初始内容（命令直接发结果；普通消息发 `⏳`）
2. **更新**：Claude 输出时，追加内容到 stream，发送更新
   - 每次发送完整累积内容（替换模式）
   - 重置 finish 定时器
3. **结束**：以下任一条件触发 finish
   - 收到 assistant text 后 5 秒无新输出
   - 用户发送新消息（老 stream 立即 finish）
   - 累积内容接近 19000 字节上限
   - 执行命令（立即 finish）

### 消息类型处理

| Claude 输出类型 | 处理方式 |
|---|---|
| text（assistant） | 追加到 stream content |
| thinking | 不发送（与自建应用模式一致） |
| tool_use | verbose=false: 不发送; verbose=true: 收集到 ToolCollector |
| tool_result | verbose=false: 不发送; verbose=true: 收集到 ToolCollector |
| tool 汇总 | verbose=true 收到 text 前先 flush ToolCollector，追加到 stream |
| image | 收集到 pending，stream finish 时通过 msg_item 发送 |

### 交互 UI 处理

**问题**：自建应用模式用模板卡片（template_card）实现按钮，但 stream 模式不支持模板卡片。

**方案**：改用 **文本提示 + 用户回复** 方式：

| 交互类型 | 处理 |
|---|---|
| Permission | 追加到 stream：`⚠️ 需要权限确认: [描述] 回复 Y 允许 / N 拒绝` |
| ExitPlanMode | 追加到 stream：`📋 计划已就绪，回复 OK 确认执行` |
| AskUserQuestion | 追加到 stream：`❓ [问题内容]` |

**响应识别**：用户回复 Y/N/OK 时，检查是否有 pending 交互状态，优先处理：

```python
_pending_interactive: dict[str, str]  # chatid → "permission" | "planmode" | "question"

# 用户发来 "Y" 且有 pending permission:
#   → tmux send_keys "y"
#   → 更新 stream: "✅ 已允许"
#   → 清除 pending 状态
```

**替代方案（response_url）**：如果希望保留按钮体验，可以用 `response_url` 发送 `template_card`。但 response_url 一次性限制意味着 **一条用户消息最多一次 template_card**。对于 ccbot 场景（一次对话可能多次 Permission），文本方案更可靠。

### 媒体操作

| 操作 | 实现方式 |
|---|---|
| 发送截图 | stream finish 时通过 `msg_item` 附带 base64 图片 |
| 发送文件 | **不支持**（stream 不支持文件），在 stream 中告知文件路径 |
| 接收图片 | 消息体中有 URL，直接 HTTP 下载 |
| 接收文件 | 消息体中有 media_id，需要 access_token 下载 |
| 接收语音 | 消息体中有 media_id，需要 access_token 下载 |

**媒体下载依赖**：接收文件/语音需要 corp_id + secret 获取 access_token。配置中将这两项设为 **可选**。未配置时：
- 图片接收：仍可用（URL 直接下载）
- 文件/语音接收：不可用，提示用户配置

### 命令处理

与自建应用模式相同，但响应方式不同：

| 命令 | 处理 |
|---|---|
| `/bind <path>` | 绑定群到目录，通过 stream 回复 |
| `/unbind` | 解绑，stream 回复 |
| `/verbose` | 切换详细模式，stream 回复 |
| `/screenshot` | 截图后通过 stream msg_item 发送 |
| `/esc` | 发送 Escape，stream 回复确认 |
| `/kill` | 终止窗口，stream 回复确认 |
| `/history` | 历史消息，stream 回复 |
| `/file <path>` | **不支持自动发送文件**，stream 中展示文件路径 |

所有命令都是 **立即 finish** 模式（创建 stream → 发送结果 → finish）。

## 架构设计

### 文件结构

```
src/ccbot/wecom/
├── __init__.py          (不变)
├── config.py            (修改：新增 bot_id, bot_secret)
├── crypto.py            (不变，bot 模式不使用)
├── client.py            (不变，bot 模式可选使用，用于媒体下载)
├── bot.py               (不变，自建应用模式)
├── ws_client.py          (新增：WebSocket 长连接管理)
└── aibot.py             (新增：智能机器人模式主入口)
```

### 模块职责

**ws_client.py — WebSocket 连接管理**
```
WeComWSClient
├── connect()                    # 连接 + 订阅 + 启动后台任务
├── send_stream_response()       # 发送 aibot_respond_msg
├── set_message_callback()       # 设置消息回调
├── close()                      # 关闭连接
│
├── _connect_ws()                # 建立连接并订阅
├── _ping_loop()                 # 心跳循环（30s）
├── _receive_loop()              # 接收帧循环
├── _handle_frame()              # 帧路由（msg_callback/pong/ack）
├── _reconnect()                 # 关闭并重连
└── _schedule_reconnect()        # 指数退避重连
```

**aibot.py — 消息路由 + 会话管理**
```
WeComAIBot
├── start() / shutdown()         # 生命周期
├── _on_ws_message()             # WebSocket 消息分发
│
├── _handle_text_message()       # 文本消息 → 转发给 Claude
├── _handle_command()            # 命令处理 (复用自建应用逻辑)
├── _handle_interactive_reply()  # Y/N/OK 交互响应
│
├── _on_new_message()            # SessionMonitor 回调 → 更新 stream
├── _find_chatid_for_session()   # session_id → chatid 反查
│
├── _create_stream()             # 创建新 stream
├── _update_stream()             # 追加内容并发送更新
├── _finish_stream()             # 结束 stream
├── _schedule_stream_finish()    # 5s 定时 finish
│
├── _ensure_window()             # tmux 窗口管理（同自建应用）
├── _restore_bindings()          # 启动时恢复绑定
├── _poll_interactive_ui()       # 交互 UI 轮询
│
└── _post_response_url()         # 备选：通过 response_url 发送
```

### 消息流

**入站（用户 → Claude）**：
```
用户在群里发消息
  → WeCom WebSocket 推送 aibot_msg_callback (JSON)
  → 解析 chatid + userid + content
  → finish 旧 stream（如果有）
  → 创建新 stream（⏳ 占位）
  → 查 wecom_groups[chatid] → window_id
  → tmux send_keys(window_id, content)
```

**出站（Claude → 用户）**：
```
SessionMonitor 检测到新 JSONL 条目
  → NewMessage callback
  → session_id → window_id → chatid
  → 过滤: 跳过 user/thinking/tool（非 verbose）
  → 追加到 chat 的 active stream content
  → 发送 aibot_respond_msg 更新 stream
  → 重置 5s finish 定时器
```

**命令（直接响应）**：
```
用户发送 "/screenshot"
  → 创建 stream
  → 截取 tmux 画面 → base64 图片
  → stream finish + msg_item 附带图片
```

### 状态管理

复用现有的状态文件，不新增：
- `wecom_groups.json` — 群绑定（chatid → cwd/name/verbose/window_id）
- `session_map.json` — hook 写入的 window → session 映射
- `state.json` — window_states, display_names（共享 SessionManager）
- `monitor_state.json` — JSONL 字节偏移

## 配置

### 新增环境变量

```env
# 智能机器人凭证（bot 模式必填）
WECOM_BOT_ID=your_bot_id
WECOM_BOT_SECRET=your_bot_secret

# 可选：用于媒体下载（文件/语音接收）
WECOM_CORP_ID=ww1234567890abcdef
WECOM_SECRET=your_corp_secret

# 可选：用户白名单
WECOM_ALLOWED_USERS=zhangsan,lisi
```

### 配置验证

bot 模式只需要 `WECOM_BOT_ID` + `WECOM_BOT_SECRET`，其他都是可选的。

```python
def validate_bot(self) -> None:
    """验证 bot 模式必要配置。"""
    missing = []
    if not self.bot_id:
        missing.append("WECOM_BOT_ID")
    if not self.bot_secret:
        missing.append("WECOM_BOT_SECRET")
    if missing:
        raise ValueError(f"Missing: {', '.join(missing)}")
```

## CLI 入口

```bash
ccbot wecom       # 自建应用模式（HTTP 回调，现有）
ccbot wecom-bot   # 智能机器人模式（WebSocket 长连接，新增）
ccbot hook        # Hook（不变）
```

## 依赖

无需新增依赖。`aiohttp`（已在 `[wecom]` optional deps 中）同时支持 HTTP server 和 WebSocket client。

## Stream 更新节流（参考 openclaw-plugin-wecom）

为避免高频 stream 更新导致 WeCom WebSocket 队列溢出，采用节流策略：

- **最小发送间隔 800ms**：两次 `aibot_respond_msg` 之间至少间隔 800ms
- 如果 content 有更新但距上次发送不足 800ms，设置 delayed send（asyncio.call_later）
- delayed send 触发时发送当前最新累积内容（自然合并多次小更新）

```python
@dataclass
class ChatStream:
    stream_id: str
    content: str = ""
    finished: bool = False
    last_send_time: float = 0       # 上次发送时间（节流用）
    pending_images: list = field(default_factory=list)  # base64 图片，finish 时发
    finish_timer: asyncio.TimerHandle | None = None
    throttle_timer: asyncio.TimerHandle | None = None   # 节流定时器
```

## 断线重连待发送队列

WebSocket 断开时，缓存未发送的 stream 更新：

- **TTL 5 分钟**，超时丢弃
- **最多 50 条**，溢出时丢弃最早的
- 重连成功后自动重发

## 消息状态 TTL 清理

每个 ChatStream 记录创建时间，定期清理超过 10 分钟未活动的 stream 状态，避免内存泄漏。

## 已知限制

1. **无法主动推送**：bot 只能在回复用户消息的上下文中发送。如果 Claude 输出时没有活跃的 stream，消息会丢失。实际影响小——用户发消息后 Claude 才开始处理。
2. **单条消息上限 20KB**：长时间运行的 Claude 任务可能产生超长输出。超出时 finish 当前 stream，后续内容等待用户新消息。
3. **文件发送不支持**：stream 不支持文件附件。只能在 stream 中展示文件路径，用户用 `/file` 手动获取——但 `/file` 本身也受限于 stream 模式（只能展示路径）。
4. **交互 UI 降级**：没有按钮卡片，改用文本提示 + Y/N 回复。体验稍差但功能完整。
5. **单用户单 stream**：同一 chat 同一时间只有一个活跃 stream。用户连续发多条消息时，前一条的 stream 会被 finish。

## 实施顺序

1. **config.py** — 新增 bot_id/bot_secret 配置 + validate_bot()
2. **ws_client.py** — WebSocket 连接、订阅、心跳、重连、pending reply queue
3. **aibot.py** — 最小链路：收文本 → tmux → Claude 输出 → stream 回复
4. **main.py** — 新增 `ccbot wecom-bot` 入口
5. **stream 生命周期** — 创建/更新/finish/定时器/节流
6. **命令处理** — /bind, /unbind, /verbose, /esc, /kill, /history, /screenshot
7. **交互 UI** — Permission/AskUserQuestion/ExitPlanMode 文本提示
8. **verbose 模式** — ToolCollector 汇总
9. **媒体操作** — 截图(msg_item)、图片接收(URL)、文件/语音(可选)

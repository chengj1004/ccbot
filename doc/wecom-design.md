# 企业微信适配方案

## 概述

在独立分支上实现企业微信（WeCom）支持。复用核心层（session/monitor/tmux），重写前端消息收发部分。不做抽象接口层，Telegram 和 WeCom 代码独立运行。

## 核心设计决策

### 1. 群聊/单聊隔离（1 会话 = 1 Window = 1 Session）

支持两种路由模式：
- **群聊**：用企微群聊 chatid 作为路由键
- **单聊**：用 `dm:{userid}` 作为虚拟 chatid，直接给应用发消息即可

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│  群聊 chatid │      │             │      │             │
│  (WeCom)    │ ───▶ │ Window ID   │ ───▶ │ Session ID  │
│  dm:userid  │      │ (tmux @id)  │      │  (Claude)   │
│  (单聊)     │      │             │      │             │
└─────────────┘      └─────────────┘      └─────────────┘
   wecom_groups.json      session_map.json
                          (written by hook)
```

### 2. 群→目录映射

**配置文件方式**（主要）：`~/.ccbot/wecom_groups.json`

```json
{
  "wrxxxxxxxx": {
    "name": "ccbot项目",
    "cwd": "/home/user/Code/ccbot",
    "verbose": false
  },
  "wryyyyyyyy": {
    "name": "前端项目",
    "cwd": "/home/user/Code/frontend",
    "verbose": false
  }
}
```

**群内命令方式**（动态）：

- `/bind /home/user/Code/project` — 绑定当前群到目录
- `/unbind` — 解绑当前群

Bot 收到群消息时，如果群未绑定且无配置，则提示使用 `/bind`。

### 3. 不发中间过程消息（方案 C）

企微不支持编辑已发消息，逐条发 tool_use/tool_result 会导致大量消息提醒。

**默认模式（verbose=false）**：
- tool_use / tool_result：静默不发
- assistant 文本：正常发送
- thinking：不发送
- status 状态行：不发送（无法编辑，发了也只能追加）

**详细模式（verbose=true，通过 `/verbose` 切换）**：
- 一轮 tool 调用结束后，汇总成一条消息发出：
  ```
  🔧 执行了 3 个工具:
  • Read: src/ccbot/bot.py
  • Edit: src/ccbot/config.py
  • Bash: uv run ruff check
  ```
- assistant 文本：正常发送
- thinking：不发送
- status 状态行：不发送

### 4. 交互 UI（模板卡片）

企微的模板卡片消息支持按钮，可用于：
- **Permission prompt**（Allow/Deny）
- **AskUserQuestion**（用户直接在群里回复文本）
- **ExitPlanMode**（确认按钮）

模板卡片可以通过 `update_template_card` API 更新状态（比如点击后变成"已允许"），这是企微中唯一类似"编辑消息"的能力。

## 与 Telegram 版的关键差异

| 特性 | Telegram | WeCom |
|---|---|---|
| 路由单元 | Forum Topic (thread_id) | 群聊 (chatid) |
| 单聊支持 | 无 (Topic-only) | 支持 (dm:{userid}) |
| 消息格式 | MarkdownV2 (严格转义) | Markdown (企微简化版，自动转换) |
| 消息编辑 | edit_message_text | 不支持（模板卡片除外） |
| 单条限制 | 4096 字符 | 2048 字节 (text) |
| 交互按钮 | InlineKeyboard | 模板卡片 button_list |
| 消息接收 | Long polling | Webhook (HTTP callback) |
| 用户标识 | user_id (int) | userid (string) |
| 状态消息 | 编辑同一条 | 不发 |
| tool_use | 编辑为 tool_result | 不发 / 汇总发 |

## 可复用的模块（不改动）

| 模块 | 用途 |
|---|---|
| `session_monitor.py` | JSONL 轮询，检测新消息 |
| `transcript_parser.py` | 解析 JSONL 内容 |
| `terminal_parser.py` | 检测交互 UI、解析状态行 |
| `tmux_manager.py` | tmux 窗口管理 |
| `monitor_state.py` | 字节偏移跟踪 |
| `hook.py` | SessionStart 钩子写 session_map.json |
| `screenshot.py` | 终端截图渲染 |
| `utils.py` | ccbot_dir, atomic_write_json |

## 新建文件

```
src/ccbot/wecom/
├── __init__.py
├── config.py          # 企微配置: corp_id, secret, agent_id, 群映射
├── crypto.py          # 回调消息加解密 (AES-CBC + 签名验证)
├── client.py          # 企微 API 客户端 (access_token, 发消息, 上传媒体)
└── bot.py             # 主入口: webhook server + 消息路由 + monitor集成
                       #   包含: 命令处理, tool汇总, 交互UI轮询, 模板卡片
```

## 架构设计

### 消息收发流程

**入站（用户 → Claude）**：

```
用户在群里发消息
  → WeCom webhook POST (加密 XML)
  → crypto.decrypt → 提取 chatid + userid + content
  → 查 wecom_groups[chatid] → window_id
  → tmux_manager.send_keys(window_id, content)
```

**出站（Claude → 用户）**：

```
SessionMonitor 检测到新 JSONL 条目
  → NewMessage callback
  → 根据 session_id 查 window_id → chatid
  → 过滤: verbose=false 时跳过 tool_use/tool_result
  → 通过 wecom client API 发送到群聊
```

### Webhook Server

使用 `aiohttp` 起 HTTP 服务器，处理两类请求：

```
GET  /callback?msg_signature=...&timestamp=...&nonce=...&echostr=...
  → URL 验证: 解密 echostr 并返回

POST /callback?msg_signature=...&timestamp=...&nonce=...
  → 消息接收: 解密 XML → 解析消息 → 路由处理
```

### Access Token 管理

```python
class WeComClient:
    async def get_access_token(self) -> str:
        # 缓存 token, 过期前 5 分钟刷新
        # GET https://qyapi.weixin.qq.com/cgi-bin/gettoken
        #   ?corpid=xxx&corpsecret=xxx
```

### 消息发送 API

```python
# 发文本
POST /cgi-bin/appchat/send
{"chatid": "wr...", "msgtype": "text", "text": {"content": "..."}}

# 发 Markdown
POST /cgi-bin/appchat/send
{"chatid": "wr...", "msgtype": "markdown", "markdown": {"content": "..."}}

# 发图片
POST /cgi-bin/media/upload  → media_id
POST /cgi-bin/appchat/send
{"chatid": "wr...", "msgtype": "image", "image": {"media_id": "..."}}

# 发模板卡片 (交互 UI)
POST /cgi-bin/appchat/send
{
  "chatid": "wr...",
  "msgtype": "template_card",
  "template_card": {
    "card_type": "button_interaction",
    "main_title": {"title": "Permission Required"},
    "sub_title_text": "Allow Read access to file.py?",
    "button_list": [
      {"text": "Allow", "key": "perm_allow_@5"},
      {"text": "Deny", "key": "perm_deny_@5"}
    ]
  }
}

# 更新卡片状态 (点击按钮后)
POST /cgi-bin/message/update_template_card
```

### Message Queue（简化版）

相比 Telegram 版，去掉了：
- 消息编辑逻辑 (edit_message_text)
- 消息删除逻辑 (delete_message)
- tool_msg_ids 追踪
- status_msg_info 追踪
- status → content 转换

保留：
- FIFO 队列 + per-group worker
- 消息合并（多条连续文本合并为一条）
- Flood control（防止发送过快）
- verbose 模式的 tool 汇总

### Verbose 模式的 Tool 汇总

Message queue worker 维护一个 per-group 的 tool 收集器：

```python
@dataclass
class ToolCollector:
    tools: list[str]  # ["Read: src/bot.py", "Edit: src/config.py"]

    def flush(self) -> str | None:
        """当收到非 tool 消息时，汇总并清空。"""
        if not self.tools:
            return None
        summary = f"🔧 执行了 {len(self.tools)} 个工具:\n"
        summary += "\n".join(f"• {t}" for t in self.tools)
        self.tools.clear()
        return summary
```

### 状态管理

`wecom_state.json`：

```json
{
  "group_bindings": {
    "wrxxxxxxxx": {
      "window_id": "@5",
      "cwd": "/home/user/Code/ccbot"
    }
  },
  "window_states": {
    "@5": {
      "session_id": "uuid-xxx",
      "cwd": "/home/user/Code/ccbot"
    }
  }
}
```

与 Telegram 版共享 `session_map.json`（hook 写入），独立维护 `wecom_state.json` 和 `wecom_monitor_state.json`。

## 群内命令

| 命令 | 说明 |
|---|---|
| `/bind <path>` | 绑定群到工作目录 |
| `/unbind` | 解绑群 |
| `/verbose` | 切换详细模式 (显示 tool 汇总) |
| `/screenshot` | 截取 tmux 终端画面 |
| `/esc` | 发送 Escape 中断 Claude |
| `/kill` | 终止 Claude 进程 |
| `/history` | 查看消息历史 (纯文本) |
| `/file <path>` | 发送文件到聊天 (支持相对/绝对路径) |

注意：企微不支持 `/` 前缀的命令自动补全，用户需要手动输入完整命令。

### 语音消息

支持语音消息输入，流程：
1. 接收企微语音消息（AMR 格式）
2. 通过企微 API 下载语音文件
3. ffmpeg 转换 AMR → MP3（OpenAI 不支持 AMR）
4. 调用 OpenAI gpt-4o-transcribe 转文字
5. 回显转写文本（`🎤 xxx`），然后作为普通文本发给 Claude

需要：
- 服务器安装 `ffmpeg`：`apt install ffmpeg`
- `.env` 配置 `OPENAI_API_KEY`（可选配 `OPENAI_BASE_URL`）

### 会话恢复

`/bind` 绑定目录时，如果该目录下存在已有的 Claude 会话，会展示会话列表供用户选择恢复或新建：

```
发现已有会话，回复数字恢复或输入 0 新建:

1. 上次的对话摘要 — 42 条消息
2. 更早的对话 — 15 条消息

0. 新建会话
```

发消息时如果窗口已关闭，也会触发同样的会话选择流程，选择后原消息自动转发。

`--resume` 模式下，hook 会报告新的 session_id，但消息实际写入原 JSONL 文件。bot 会将 window_state 的 session_id 覆盖回原始值，确保 monitor 跟踪正确的文件。

### 文件自动发送

当 Claude 的回复文本中提到文档文件路径（绝对路径）且文件存在时，bot 自动上传并发送文件。同时也监听 `Write` 工具创建的文档文件。

支持的文件类型：`.docx` `.pdf` `.xlsx` `.csv` `.pptx` `.zip` `.html` `.htm` `.md` `.txt` `.json` `.yaml` 等。

也可通过 `/file <path>` 手动发送任意文件（20MB 以内）。

### 接收用户文件

用户在聊天中发送文件或图片时，bot 自动下载并保存到绑定目录的 `uploads/` 子目录下，然后通知 Claude 文件已保存。同名文件自动加后缀避免覆盖。

### Markdown 转换

所有消息通过 `_to_wecom_markdown()` 转换后以 markdown 消息类型发送：
- `# 标题` → `**标题**`（企微不支持标题语法）
- 代码块 → `> ` 引用格式（企微不支持围栏代码块）
- 加粗、链接、行内代码、引用等保持原样

## CLI 入口

```bash
ccbot run       # Telegram bot (默认，现有行为)
ccbot wecom     # WeCom bot
ccbot hook      # Hook (不变)
```

## 依赖新增

```toml
[project.optional-dependencies]
wecom = [
    "aiohttp>=3.9.0",       # Webhook server
    "cryptography>=42.0.0",  # AES 加解密
]
```

使用 optional dependency 避免影响 Telegram-only 用户。

## 企业微信后台配置

### 第一步：创建自建应用

1. 登录 [企业微信管理后台](https://work.weixin.qq.com/wework_admin/frame)
2. 进入 **应用管理** → **自建** → **创建应用**
3. 填写应用信息：
   - 应用名称：如 `Claude Bot`
   - 应用logo：随意
   - 可见范围：选择需要使用的部门或成员
4. 创建完成后，在应用详情页记录：
   - **AgentId**：应用的 AgentId（如 `1000002`）
   - **Secret**：点击查看获取，这是 `WECOM_SECRET`

### 第二步：获取企业 ID

1. 在管理后台 **我的企业** → 最下方找到 **企业ID**
2. 格式如 `ww1234567890abcdef`，这是 `WECOM_CORP_ID`

### 第三步：配置接收消息

1. 在应用详情页，找到 **接收消息** → **设置API接收**
2. 填写：
   - **URL**：你的服务器回调地址，如 `https://your-server.com/callback`
   - **Token**：点击随机获取，记录为 `WECOM_CALLBACK_TOKEN`
   - **EncodingAESKey**：点击随机获取，记录为 `WECOM_ENCODING_AES_KEY`
3. 点击保存前，需要先启动 ccbot wecom 服务（因为保存时企微会发送验证请求）

> **回调 URL 必须是公网可访问的 HTTPS 地址。** 开发阶段可以用 ngrok/frp 等内网穿透工具：
> ```bash
> # 示例：用 ngrok 暴露本地 8080 端口
> ngrok http 8080
> # 拿到的 https://xxxx.ngrok.io/callback 填入企微后台
> ```

### 第四步：使用方式

支持两种使用方式：

**方式 A：单聊（推荐）**

直接在企业微信中找到你的应用，发送 `/bind /path/to/project` 即可。无需创建群聊。

**方式 B：群聊**

通过 API 创建应用群聊，或在已有群聊中使用 `/bind` 命令。

> 注意：应用群聊至少需要 2 个成员才能创建。

### 第五步：配置应用可信域名（可选）

如果需要使用模板卡片（交互按钮），需要在应用详情页 → **网页授权及JS-SDK** 中配置可信域名。

### 第六步：配置 ccbot

将上面获取的信息写入 `~/.ccbot/.env`：

```env
# 企业微信凭证
WECOM_CORP_ID=ww1234567890abcdef
WECOM_SECRET=your_app_secret_here
WECOM_AGENT_ID=1000002

# 回调验证（第三步获取）
WECOM_CALLBACK_TOKEN=your_callback_token
WECOM_ENCODING_AES_KEY=your_43char_encoding_aes_key

# 可选配置
WECOM_LISTEN_HOST=0.0.0.0     # 监听地址，默认 0.0.0.0
WECOM_LISTEN_PORT=8080         # 监听端口，默认 8080
WECOM_ALLOWED_USERS=zhangsan,lisi  # 允许使用的用户ID，留空则不限制

# 语音转文字（可选，需要 ffmpeg）
OPENAI_API_KEY=your_openai_api_key
# OPENAI_BASE_URL=https://api.openai.com/v1

# 共享配置
TMUX_SESSION_NAME=ccbot
CLAUDE_COMMAND=claude
```

### 运行模式切换

企微启动的 Claude Code 支持两种运行模式，通过 `.env` 中的 `CLAUDE_COMMAND` 切换：

**模式 A：默认订阅模式**

使用你本地登录的 Claude Max/Pro 订阅额度：

```env
CLAUDE_COMMAND=claude --dangerously-skip-permissions
```

优点：配置简单，无需额外搭建。缺点：与本地使用共享订阅额度。

**模式 B：API 代理模式**

通过独立的 API 代理运行，与本地订阅完全隔离：

```env
CLAUDE_COMMAND=env CLAUDE_CONFIG_DIR=~/.claude-wecom claude --dangerously-skip-permissions
```

可在末尾追加 system prompt 限制操作范围：
```env
CLAUDE_COMMAND=env CLAUDE_CONFIG_DIR=~/.claude-wecom claude --dangerously-skip-permissions --append-system-prompt "你只能操作当前工作目录下的文件，禁止读写或访问其他项目目录。"
```

API 代理模式需要额外配置：

1. 创建独立配置目录 `~/.claude-wecom/`
2. 创建 `~/.claude-wecom/settings.json`：
   ```json
   {
       "env": {
           "ANTHROPIC_BASE_URL": "http://your-proxy:6666",
           "ANTHROPIC_AUTH_TOKEN": "your-api-token"
       },
       "hooks": {
           "SessionStart": [
               {
                   "hooks": [
                       {
                           "type": "command",
                           "command": "/path/to/ccbot hook",
                           "timeout": 5
                       }
                   ]
               }
           ]
       },
       "skipDangerousModePermissionPrompt": true
   }
   ```
3. 创建 `~/.claude-wecom/.claude.json`，确保包含 `"hasCompletedOnboarding": true`
4. 符号链接 projects 目录：`ln -sf ~/.claude/projects ~/.claude-wecom/projects`

这样企微的 Claude Code 走 API 代理计费，本地的 Claude Code 继续用订阅，互不影响。

> **切换模式后**：需要重启 ccbot wecom 服务，并对已绑定的会话执行 `/kill`，下次发消息时会用新模式重建窗口。

配置群聊绑定 `~/.ccbot/wecom_groups.json`：

```json
{
  "ccbot_project1": {
    "name": "ccbot项目",
    "cwd": "/home/user/Code/ccbot"
  },
  "ccbot_frontend": {
    "name": "前端项目",
    "cwd": "/home/user/Code/frontend"
  }
}
```

### 第七步：启动

```bash
# 安装系统依赖（语音转文字需要）
apt install ffmpeg

# 安装 wecom 依赖
uv sync --extra wecom

# 安装 Claude Code hook（如果还没装）
ccbot hook --install

# 启动 WeCom bot
ccbot wecom
```

启动后会在 `0.0.0.0:8080` 监听 webhook 回调。此时可以回到企微后台完成第三步的保存验证。

### 完整流程图

```
企微管理后台                        你的服务器
┌──────────────────┐              ┌──────────────────────┐
│ 1. 创建自建应用    │              │                      │
│ 2. 获取企业ID      │              │  ccbot wecom         │
│ 3. 设置API接收 ────┼── 验证请求 ──→│  (aiohttp :8080)     │
│    Token           │← 解密返回 ──┤                      │
│    EncodingAESKey  │              │                      │
│ 4. 创建应用群聊    │              │  tmux session        │
│                    │              │  ├─ window @5 (claude)│
│                    │              │  └─ window @8 (claude)│
└──────────────────┘              └──────────────────────┘

用户在群里发消息
┌──────────────┐    加密POST     ┌──────────────────────┐
│ 企微群聊      │───────────────→│ webhook /callback     │
│ chatid=xxx   │                 │ → 解密 → 路由        │
│              │← appchat/send ─│ → tmux send_keys     │
│              │   (Claude回复)  │ ← session monitor    │
└──────────────┘                 └──────────────────────┘
```

### 常见问题

**Q: 回调 URL 验证失败？**
- 确保 ccbot wecom 已启动且端口可达
- 确保 URL 是 HTTPS（企微要求）
- 检查 Token 和 EncodingAESKey 是否复制正确（不要有多余空格）

**Q: 消息发不到群里/单聊？**
- 确认应用的 Secret 正确
- 确认服务器出口 IP 已加入企微后台的 **企业可信IP** 白名单
- 群聊模式下，确认使用的是 **应用群聊**（通过 API 创建），不是普通群聊

**Q: 消息解密失败 (Invalid padding bytes)？**
- 确认 `.env` 中的 `WECOM_CALLBACK_TOKEN` 和 `WECOM_ENCODING_AES_KEY` 与企微后台一致
- 修改后需要重启 bot

**Q: 发消息 bot 没反应？**
- 确认 `WECOM_ALLOWED_USERS` 为空（不限制）或包含你的 userid
- 查看 ccbot 日志确认是否收到 webhook 回调

**Q: 如何获取自己的企微 userid？**
- 管理后台 → **通讯录** → 点击成员 → **账号** 字段即为 userid

## 实施顺序

1. **最小链路**：群收消息 → tmux → claude 输出 → 纯文本发回群
2. **Markdown 格式化**：企微 markdown 格式转换
3. **交互 UI**：模板卡片实现 Permission / AskUserQuestion
4. **Verbose 模式**：tool 汇总功能
5. **截图**：复用 screenshot.py + 企微图片消息
6. **群管理命令**：/bind, /unbind, /verbose 等

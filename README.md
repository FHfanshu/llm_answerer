# LLM Answerer

一个 OpenAI-compatible 的本地答题服务，提供 OCS `AnswererWrapper` 兼容 HTTP 接口。

当前版本主打低延迟和低成本：先查本地题库，再查 SQLite 缓存，未命中时才调用 LLM。Prompt 使用固定 system 前缀，把动态题目放在最后，便于服务端 prefix/KV cache 命中。

## 功能

- 兼容 OCS `AnswererWrapper`
- 支持单选、多选、判断、填空
- 本地 `question_bank.json` 题库缓存
- SQLite 二级缓存
- FastAPI 异步接口，可处理并发请求
- Rich 终端 dashboard，显示命中率、请求来源、估算 token 和平均耗时
- 支持 OpenAI-compatible API，例如 DeepSeek、OpenRouter、火山等
- 可选 Tavily/Exa 搜索模块保留在 `search.py`，主服务默认不走置信度和联网搜索

## 安装

```bash
uv venv
uv pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell:

```powershell
uv venv
uv pip install -r requirements.txt
Copy-Item .env.example .env
```

## 配置

编辑 `.env`：

```env
OPENAI_API_KEY=your-api-key-here
OPENAI_MODEL=deepseek-chat
OPENAI_BASE_URL=https://api.deepseek.com/v1

# 纯知识题建议留空，复杂推理题可设 low/medium/high
REASONING_EFFORT=

# 可选
# LISTEN_PORT=5000
# ACCESS_TOKEN=your_secret_token_here
# CACHE_RETRY_PROBABILITY=0.1
```

DeepSeek reasoning 示例：

```env
OPENAI_MODEL=deepseek-v4-pro
REASONING_EFFORT=low
```

纯知识题不建议开启 reasoning，通常会增加延迟和 token 消耗。

## 启动

```bash
python llm_answerer.py
```

Windows PowerShell:

```powershell
.venv\Scripts\python.exe llm_answerer.py
```

启动后终端会显示 Rich dashboard：

- 总请求数
- 题库命中数
- SQLite 命中数
- 总命中率
- 估算 token 消耗
- 平均耗时
- 最近请求日志

## OCS 配置

在 OCS 浏览器脚本的「题库配置」中添加：

```json
[
  {
    "name": "LLM智能答题",
    "url": "http://localhost:5000/search",
    "method": "post",
    "contentType": "json",
    "type": "GM_xmlhttpRequest",
    "headers": {
      "Content-Type": "application/json"
    },
    "data": {
      "title": "${title}",
      "options": "${options}",
      "type": "${type}"
    },
    "handler": "return (res) => res.code === 1 ? [undefined, res.answer] : [res.msg, undefined]"
  }
]
```

如果启用了 `ACCESS_TOKEN`，需要把 token 加进 headers：

```json
"headers": {
  "Content-Type": "application/json",
  "X-Access-Token": "your_secret_token_here"
}
```

## OCS 并发设置

OCS 默认逐题调用题库接口。后端可以并发处理请求，但需要在 OCS 设置里把「线程数量（个）」从 `1` 改大。

建议：

- 「线程数量（个）」：`3` 或 `4`
- 「搜题间隔（秒）」：`0` 到 `1`
- 「搜题最大耗时（秒）」：`120`
- 「题库缓存功能」：开启
- 「答案匹配模式」：相似匹配

题库 JSON 仍然使用 `/search`。`/batch` 是额外接口，OCS 默认 `AnswererWrapper` 不会自动使用它，除非改 OCS 脚本源码。

## API

### 健康检查

```http
GET /
HEAD /
```

响应：

```text
ok
```

### 单题接口

```http
POST /search
```

请求：

```json
{
  "title": "Python 是一种什么类型的语言？",
  "options": "A.编译型\nB.解释型\nC.汇编型\nD.机器语言",
  "type": "single"
}
```

响应：

```json
{
  "code": 1,
  "question": "Python 是一种什么类型的语言？",
  "answer": "B"
}
```

支持 GET 参数：`title`、`options`、`type`、`skip_cache`、`token`。

### 批量接口

```http
POST /batch
```

请求：

```json
{
  "questions": [
    {
      "title": "Python 是一种什么类型的语言？",
      "options": "A.编译型\nB.解释型\nC.汇编型\nD.机器语言",
      "type": "single"
    },
    {
      "title": "地球是圆的。",
      "type": "judgement"
    }
  ]
}
```

响应：

```json
{
  "code": 1,
  "results": [
    {
      "code": 1,
      "question": "Python 是一种什么类型的语言？",
      "answer": "B"
    },
    {
      "code": 1,
      "question": "地球是圆的。",
      "answer": "正确"
    }
  ]
}
```

### 统计接口

```http
GET /stats
```

示例响应：

```json
{
  "total_requests": 50,
  "bank_hits": 35,
  "db_hits": 10,
  "hit_rate": "90.0%",
  "bank_size": 120,
  "context_window": 5
}
```

## 缓存策略

请求流程：

```text
请求
  ↓
归一化题目并生成 hash
  ↓
question_bank.json 内存题库
  ↓ 未命中
SQLite 缓存
  ↓ 未命中
LLM API
  ↓
写入 SQLite 和 question_bank.json
```

题目归一化会处理：

- HTML entity 解码
- HTML 标签移除
- 不间断空格替换
- 连续空白合并
- 常见中英文标点统一
- 大小写统一

`question_bank.json` 和 `answer_cache.db*` 默认被 `.gitignore` 排除，避免把本地题库或缓存提交到仓库。

## Prompt Cache 优化

服务端调用采用：

```text
system: 长固定规则和示例
user: 当前题型、题干、选项
```

固定内容尽量放在 `system`，动态题目只放在最后的 `user` 消息中，便于 OpenAI-compatible 服务端进行 prefix/KV cache 复用。

## 文件说明

```text
llm_answerer.py   主服务，OCS 接口、缓存、并发处理
dashboard.py      Rich 终端状态面板
search.py         Tavily/Exa 搜索封装，默认主服务不调用
confidence.py     旧置信度增强逻辑，当前精简主服务不依赖
.env.example      环境变量模板
requirements.txt  Python 依赖
```

## 注意

请勿提交 `.env`、`question_bank.json`、`answer_cache.db*`。这些文件可能包含 API key、本地题库或运行数据。

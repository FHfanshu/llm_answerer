"""
LLM 答题服务 - 精简版
直接调用 LLM 返回答案，支持并发，带缓存
"""
import os
import sys
import hashlib
import json
import asyncio
import random
import re
import html
import time
from contextlib import asynccontextmanager
from typing import Optional

import aiosqlite
from openai import AsyncOpenAI
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import uvicorn
from rich.console import Console
from rich.live import Live

from dashboard import dashboard

# UTF-8 编码
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    os.system('chcp 65001 >nul 2>&1')
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

load_dotenv()

# 配置
API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "deepseek-chat")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
PORT = int(os.getenv("LISTEN_PORT", "5000"))
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
CACHE_RETRY_PROBABILITY = float(os.getenv("CACHE_RETRY_PROBABILITY", "0.1"))
DB_PATH = os.getenv("DB_PATH", "answer_cache.db")
QUESTION_BANK_PATH = os.getenv("QUESTION_BANK_PATH", "question_bank.json")

# Reasoning Effort
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "").lower()

# 初始化 LLM 客户端
client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)


def get_llm_params() -> dict:
    """获取 LLM 调用额外参数"""
    params = {}
    if REASONING_EFFORT in ("low", "medium", "high"):
        params["reasoning_effort"] = REASONING_EFFORT
        params["extra_body"] = {"thinking": {"type": "enabled"}}
    elif "deepseek" in BASE_URL.lower():
        # DeepSeek reasoning models may return empty content unless thinking is explicit.
        params["extra_body"] = {"thinking": {"type": "disabled"}}
    return params


# ========== 数据库 ==========
class CacheDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute('PRAGMA journal_mode=WAL')
        await self.conn.execute('PRAGMA cache_size=-64000')
        await self.conn.execute('PRAGMA synchronous=NORMAL')
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS answer_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_hash TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                options TEXT,
                question_type TEXT,
                answer TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await self.conn.execute('CREATE INDEX IF NOT EXISTS idx_hash ON answer_cache(question_hash)')
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def get(self, key: str) -> Optional[str]:
        cursor = await self.conn.execute('SELECT answer FROM answer_cache WHERE question_hash = ?', (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set(self, key: str, title: str, options: str, qtype: str, answer: str):
        try:
            await self.conn.execute(
                'INSERT OR REPLACE INTO answer_cache (question_hash, title, options, question_type, answer) VALUES (?, ?, ?, ?, ?)',
                (key, title, options, qtype, answer)
            )
            await self.conn.commit()
        except Exception as e:
            print(f"[缓存写入失败] {e}")

    async def delete(self, key: str):
        if not self.conn:
            return
        await self.conn.execute('DELETE FROM answer_cache WHERE question_hash = ?', (key,))
        await self.conn.commit()


db = CacheDB(DB_PATH)

# 请求统计
stats = {"total": 0, "cache_hit": 0, "bank_hit": 0}

# 题库文件（JSON）
question_bank: dict[str, str] = {}  # hash -> answer


def load_question_bank():
    """加载题库文件"""
    global question_bank
    if os.path.exists(QUESTION_BANK_PATH):
        try:
            with open(QUESTION_BANK_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 兼容新旧格式
                if isinstance(data, dict):
                    question_bank = data
                elif isinstance(data, list):
                    # 旧格式：[{hash, answer, title, ...}, ...]
                    for item in data:
                        if "hash" in item and "answer" in item:
                            question_bank[item["hash"]] = item["answer"]
            print(f"[题库] 已加载 {len(question_bank)} 条记录")
        except Exception as e:
            print(f"[题库] 加载失败: {e}")


def save_question_bank():
    """保存题库文件"""
    try:
        with open(QUESTION_BANK_PATH, 'w', encoding='utf-8') as f:
            json.dump(question_bank, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[题库] 保存失败: {e}")


def add_to_bank(key: str, answer: str):
    """添加到题库"""
    if is_cacheable_answer(answer) and key not in question_bank:
        question_bank[key] = answer
        # 每 10 条保存一次
        if len(question_bank) % 10 == 0:
            save_question_bank()

# 上下文滑动窗口（最近的 Q&A 对）
CONTEXT_WINDOW_SIZE = 5
context_window: list[dict] = []  # [{"q": "题目", "a": "答案"}, ...]


def add_context(title: str, answer: str):
    """添加到上下文窗口"""
    context_window.append({"q": title, "a": answer})
    if len(context_window) > CONTEXT_WINDOW_SIZE:
        context_window.pop(0)


def get_context_text() -> str:
    """获取上下文文本"""
    if not context_window:
        return ""
    lines = ["最近答过的题目："]
    for i, item in enumerate(context_window, 1):
        lines.append(f"{i}. {item['q']} → {item['a']}")
    return "\n".join(lines)


def cache_key(title: str, options: Optional[str] = None) -> str:
    """生成缓存键，规范化输入以提高命中率"""
    def normalize(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)  # HTML 实体解码
        text = re.sub(r"<[^>]+>", "", text)  # 去 HTML 标签
        text = text.replace("\u00a0", " ")  # 不间断空格
        text = re.sub(r"\s+", " ", text)  # 合并空格
        text = text.strip().lower()
        # 统一标点
        text = text.replace('（', '(').replace('）', ')')
        text = text.replace('，', ',').replace('。', '.')
        text = text.replace('：', ':').replace('；', ';')
        text = text.replace('"', '"').replace('"', '"')
        text = text.replace(''', "'").replace(''', "'")
        return text

    norm_title = normalize(title)
    norm_options = normalize(options) if options else ""
    return hashlib.sha256(f"{norm_title}|{norm_options}".encode()).hexdigest()


# ========== Prompt（长固定前缀 + 题目放最后） ==========
SYSTEM_PROMPT = """你是一个用于自学练习的题目解析助手。你需要根据题干和选项给出答案。

必须遵守以下规则：
1. 只输出答案，不要输出任何解释。
2. 单选题：只输出一个选项字母（如 A）。
3. 多选题：输出所有正确选项字母，用 # 分隔（如 A#C#D）。
4. 判断题：只输出"正确"或"错误"。
5. 填空题：直接输出答案，多个空用 # 分隔。
6. 不确定时也要给出最可能的答案。

示例：
题型：single
题干：1+1=？
选项：A. 1  B. 2  C. 3  D. 4
答案：B

题型：judgement
题干：地球是圆的
答案：正确

题型：multiple
题干：以下哪些是编程语言？
选项：A. Python  B. HTML  C. Java  D. CSS
答案：A#C
"""


def format_options(options: Optional[str]) -> str:
    """Ensure options have stable A/B/C labels for LLM responses."""
    if not options:
        return ""

    lines = [line.strip() for line in options.splitlines() if line.strip()]
    if not lines:
        return ""

    formatted = []
    for index, line in enumerate(lines):
        if re.match(r"^[A-Za-z][\.、\)]\s*", line):
            formatted.append(line)
            continue
        label = chr(ord("A") + index)
        formatted.append(f"{label}. {line}")
    return "\n".join(formatted)


def build_user_message(title: str, options: Optional[str] = None, qtype: Optional[str] = None) -> str:
    """构建用户消息（动态部分，放最后）"""
    msg = f"题型：{normalize_question_type(qtype) or 'unknown'}\n题干：{title}"
    formatted_options = format_options(options)
    if formatted_options:
        msg += f"\n选项：\n{formatted_options}"
    return msg


# ========== 答案验证 ==========
def normalize_question_type(qtype: Optional[str]) -> Optional[str]:
    if not qtype:
        return None
    value = qtype.strip().lower()
    if value in ("single", "radio") or "单选" in value:
        return "single"
    if value in ("multiple", "checkbox") or "多选" in value:
        return "multiple"
    if value in ("judgement", "judge", "judgment", "boolean", "truefalse") or "判断" in value:
        return "judgement"
    if value in ("completion", "blank", "fill") or "填空" in value:
        return "completion"
    return value


def option_labels(options: Optional[str]) -> list[str]:
    formatted = format_options(options)
    labels = []
    for line in formatted.splitlines():
        match = re.match(r"^([A-Z])\.\s*", line.upper())
        if match:
            labels.append(match.group(1))
    return labels


def option_text_map(options: Optional[str]) -> dict[str, str]:
    mapping = {}
    formatted = format_options(options)
    for line in formatted.splitlines():
        match = re.match(r"^([A-Z])\.\s*(.*)$", line, re.IGNORECASE)
        if match:
            mapping[match.group(1).upper()] = cache_normalize(match.group(2))
    return mapping


def cache_normalize(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    text = text.strip().lower()
    text = text.replace('（', '(').replace('）', ')')
    text = text.replace('，', ',').replace('。', '.')
    text = text.replace('：', ':').replace('；', ';')
    return text


def validate_answer(answer: str, qtype: Optional[str]) -> bool:
    if not answer or not answer.strip():
        return False
    qtype = normalize_question_type(qtype)
    answer = answer.strip().upper()
    if qtype == "single":
        return len(answer) == 1 and "A" <= answer <= "Z"
    elif qtype == "multiple":
        parts = [part.strip().upper() for part in answer.split('#')]
        return all(len(part) == 1 and "A" <= part <= "Z" for part in parts)
    elif qtype == "judgement":
        return answer in ["正确", "错误"]
    return True


def normalize_answer(answer: str, qtype: Optional[str]) -> str:
    answer = answer.strip()
    if normalize_question_type(qtype) in ("single", "multiple"):
        return answer.upper().replace(" ", "")
    return answer


def extract_answer(raw_answer: str, qtype: Optional[str], options: Optional[str]) -> str:
    answer = raw_answer.strip()
    normalized_type = normalize_question_type(qtype)

    if normalized_type == "judgement":
        lowered = answer.lower()
        if any(token in lowered for token in ("正确", "对", "true", "yes")):
            return "正确"
        if any(token in lowered for token in ("错误", "错", "false", "no")):
            return "错误"
        return answer

    labels = option_labels(options)
    if normalized_type == "single":
        candidates = re.findall(r"(?<![A-Z])([A-Z])(?![A-Z])", answer.upper())
        for candidate in candidates:
            if not labels or candidate in labels:
                return candidate

        normalized_answer = cache_normalize(answer)
        for label, option_text in option_text_map(options).items():
            if option_text and (option_text in normalized_answer or normalized_answer in option_text):
                return label
        return normalize_answer(answer, normalized_type)

    if normalized_type == "multiple":
        candidates = re.findall(r"(?<![A-Z])([A-Z])(?![A-Z])", answer.upper())
        selected = []
        for candidate in candidates:
            if (not labels or candidate in labels) and candidate not in selected:
                selected.append(candidate)
        if selected:
            return "#".join(selected)
        return normalize_answer(answer, normalized_type)

    return answer


BAD_ANSWERS = {"", "无", "none", "null", "调用失败", "api调用失败"}


def is_cacheable_answer(answer: Optional[str]) -> bool:
    if answer is None:
        return False
    return answer.strip().lower() not in BAD_ANSWERS


def is_valid_cached_answer(answer: Optional[str], qtype: Optional[str]) -> bool:
    if not is_cacheable_answer(answer):
        return False
    return validate_answer(answer or "", qtype)


# ========== LLM 调用（带重试验证） ==========
async def call_llm(title: str, options: str = None, qtype: str = None) -> str:
    """调用 LLM 获取答案，格式不对自动重试"""
    user_msg = build_user_message(title, options, qtype)
    extra_params = get_llm_params()
    answer = "调用失败"

    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},  # 固定前缀，所有题一样
                    {"role": "user", "content": user_msg}  # 动态题目，放最后
                ],
                temperature=0.1,
                max_tokens=100,
                **extra_params
            )
            content = resp.choices[0].message.content
            if content:
                answer = extract_answer(content, qtype, options)
            if validate_answer(answer, qtype):
                return answer
            print(f"[格式验证失败] 尝试 {attempt + 1}/3: {answer}")
        except Exception as e:
            print(f"[API调用失败] 尝试 {attempt + 1}/3: {e}")
        if attempt < 2:
            await asyncio.sleep(0.5)

    return answer


# ========== FastAPI ==========
console = Console()
live: Optional[Live] = None


def refresh_dashboard():
    """刷新 dashboard 显示"""
    if live:
        live.update(dashboard.render(MODEL, PORT, len(question_bank)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global live
    await db.connect()
    load_question_bank()

    # 启动 Rich Live 显示
    live = Live(
        dashboard.render(MODEL, PORT, len(question_bank)),
        console=console,
        refresh_per_second=1,
        screen=True
    )
    live.start()

    yield

    save_question_bank()
    await db.close()
    if live:
        live.stop()


app = FastAPI(lifespan=lifespan)


@app.get('/')
@app.head('/')
async def heartbeat():
    return "ok"


@app.get('/stats')
async def get_stats():
    """缓存统计"""
    total_hits = stats["bank_hit"] + stats["cache_hit"]
    rate = total_hits / stats["total"] * 100 if stats["total"] > 0 else 0
    return {
        "total_requests": stats["total"],
        "bank_hits": stats["bank_hit"],
        "db_hits": stats["cache_hit"],
        "hit_rate": f"{rate:.1f}%",
        "bank_size": len(question_bank),
        "context_window": len(context_window)
    }


@app.get('/search')
@app.post('/search')
async def search(request: Request):
    # 解析参数
    if request.method == 'GET':
        params = dict(request.query_params)
        title = params.get('title', '')
        options = params.get('options')
        qtype = params.get('type')
        skip_cache = params.get('skip_cache', 'false').lower() == 'true'
        token = request.headers.get('X-Access-Token') or params.get('token')
        # Windows 中文编码修复
        if sys.platform == 'win32':
            for k in ('title', 'options'):
                if locals()[k]:
                    try:
                        locals()[k] = locals()[k].encode('latin1').decode('utf-8')
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        pass
    else:
        data = await request.json()
        title = data.get('title', '')
        options = data.get('options')
        qtype = data.get('type')
        skip_cache = data.get('skip_cache', False)
        token = request.headers.get('X-Access-Token') or data.get('token')

    # 鉴权
    if ACCESS_TOKEN and token != ACCESS_TOKEN:
        return JSONResponse({"code": 0, "msg": "无效的访问令牌"}, status_code=401)

    if not title:
        return JSONResponse({"code": 0, "msg": "题目不能为空"})

    key = cache_key(title, options)
    stats["total"] += 1

    # 1. 先查题库文件（内存，最快）
    if not skip_cache and key in question_bank:
        answer = question_bank[key]
        if not is_valid_cached_answer(answer, qtype):
            question_bank.pop(key, None)
            save_question_bank()
        else:
            stats["bank_hit"] += 1
            dashboard.record(title, answer, 0.001, "bank")
            refresh_dashboard()
            return JSONResponse({"code": 1, "question": title, "answer": answer})

    # 2. 再查数据库缓存
    if not skip_cache:
        cached = await db.get(key)
        if cached:
            if not is_valid_cached_answer(cached, qtype):
                await db.delete(key)
                cached = None
        if cached:
            if random.random() >= CACHE_RETRY_PROBABILITY:
                stats["cache_hit"] += 1
                add_to_bank(key, cached)  # 同步到题库
                dashboard.record(title, cached, 0.005, "db")
                refresh_dashboard()
                return JSONResponse({"code": 1, "question": title, "answer": cached})

    # 调用 LLM
    start = time.time()
    answer = await call_llm(title, options, qtype)
    elapsed = time.time() - start

    if not is_valid_cached_answer(answer, qtype):
        dashboard.record(title, "失败", elapsed, "error", 0)
        refresh_dashboard()
        return JSONResponse({"code": 0, "msg": "LLM未返回有效答案"})

    # 估算 token（粗略：中文 1 字 ≈ 2 token）
    est_tokens = len(title) * 2 + 50

    # 写缓存 + 题库 + 上下文
    await db.set(key, title, options, qtype, answer)
    add_to_bank(key, answer)
    add_context(title, answer)

    dashboard.record(title, answer, elapsed, "llm", est_tokens)
    refresh_dashboard()

    return JSONResponse({"code": 1, "question": title, "answer": answer})


@app.post('/batch')
async def batch_search(request: Request):
    """批量答题接口 - 并发处理多题"""
    data = await request.json()
    questions = data.get("questions", [])

    if not questions:
        return JSONResponse({"code": 0, "msg": "题目列表为空"})

    # 鉴权
    token = request.headers.get('X-Access-Token') or data.get('token')
    if ACCESS_TOKEN and token != ACCESS_TOKEN:
        return JSONResponse({"code": 0, "msg": "无效的访问令牌"}, status_code=401)

    async def process_one(q: dict) -> dict:
        title = q.get("title", "")
        options = q.get("options")
        qtype = q.get("type")
        key = cache_key(title, options)

        # 先查题库
        if key in question_bank:
            answer = question_bank[key]
            if is_valid_cached_answer(answer, qtype):
                stats["bank_hit"] += 1
                dashboard.record(title, answer, 0.001, "bank")
                return {"code": 1, "question": title, "answer": answer}
            question_bank.pop(key, None)

        # 再查数据库
        cached = await db.get(key)
        if cached and not is_valid_cached_answer(cached, qtype):
            await db.delete(key)
            cached = None
        if cached and random.random() >= CACHE_RETRY_PROBABILITY:
            stats["cache_hit"] += 1
            add_to_bank(key, cached)
            dashboard.record(title, cached, 0.005, "db")
            return {"code": 1, "question": title, "answer": cached}

        # 调用 LLM
        start = time.time()
        answer = await call_llm(title, options, qtype)
        elapsed = time.time() - start
        if not is_valid_cached_answer(answer, qtype):
            dashboard.record(title, "失败", elapsed, "error", 0)
            return {"code": 0, "question": title, "msg": "LLM未返回有效答案"}
        est_tokens = len(title) * 2 + 50

        await db.set(key, title, options or "", qtype or "", answer)
        add_to_bank(key, answer)
        add_context(title, answer)
        dashboard.record(title, answer, elapsed, "llm", est_tokens)
        return {"code": 1, "question": title, "answer": answer}

    stats["total"] += len(questions)
    print(f"\n[批量请求] {len(questions)} 题")

    # 并发处理
    results = await asyncio.gather(*[process_one(q) for q in questions])

    refresh_dashboard()

    return JSONResponse({"code": 1, "results": list(results)})


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=PORT, workers=1)

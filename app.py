# -*- coding: utf-8 -*-
"""AI 模拟面试官（6 评委多模型版）—— Streamlit + 6 家大模型 API。

核心玩法：
    0. 真实性校验 + 输入纠错：点「开始面试」后先调 DeepSeek 校验——
       ① 上传了简历：判断是不是一份有效简历（姓名 / 教育 / 工作或项目 / 技能等）；
       ② 岗位 / 公司：判断是否真实存在，判断不出来就给 3 个最接近的真实候选；
       校验不通过（非简历 / 明显虚构）时用黄色 st.warning 提示并禁止开始面试；
    1. 出题（每一题，分层调用）：只在 3 个「快模型」（GLM / 文心 / Kimi）里随机抽 1 位
       当「主考官」按岗位方向 + 模式出题（Java 后端 / 前端等各有独立出题范围），
       由 5 位评审团（3 个快模型 + DeepSeek + 通义）并发投票 {"pass", "reason"}，≥3 票通过
       才把题发给用户；不通过就让投反对票的评委重写 1 次并再投票，仍不通过则使用主考官的原题。
       出题 / 投票都限制 max_tokens=200。
    2. 评分：用户作答后 6 位评委并发**全量**按四个维度打分
       （技术深度 3 / 项目经验 3 / 沟通表达 2 / 逻辑思维 2，四项相加 = 本题得分），
       评分期间每位评委一张卡片显示「⏳ 正在评分...」，评完后展示四维度分与评语；
       6 位评委全部结束后才显示最终平均分（去掉一个最高分、一个最低分后取平均）；
       6 个 continue 里 ≥2 个 false 就提前结束面试（并直接出报告）。
    2.1 快速模式：设置页勾选「快速模式（用小模型，快但简单）」后，只用 GLM 一个模型
       完成所有出题 + 打分（不设独立评审团），用质量换速度。
    2.2 超时：每次 API 调用 30 秒超时，超时就跳过这位评委，不阻塞整场面试。
    3. 反问环节：问完后可向该公司的面试官提问（薪资 / 加班 / 团队 / 晋升）。
    4. 报告页：逐题展示主考官、5 位评审的投票、6 位评委的独立分数 / 四维度分 / 评语，
       最终平均分高亮，分维度展示哪块弱，并给出定位到具体题目与书名章节的改进建议。
    5. 历史记录：每次生成报告自动写入项目目录的 interview.db（SQLite），
       页面右上角「历史记录」按分数查看全部记录，并用折线图看进步趋势。

保留功能：岗位输入、面试模式、公司规模、目标企业名称、PDF 简历（PyPDF2）解析、
动态题量（最少 3 题 / 最多 8 题）、反问环节、Markdown 报告下载。
所有需要多评委参与的 API 调用都用 concurrent.futures 并发执行。
调用温度按环节设定（出题 / 评审 / 打分 / 简历匹配度 1.0、反问 0.8、综合点评 0.7、
自检 0.0、真实性校验 0.0）；Kimi 例外，它的所有调用固定 temperature=0.6
（见 JUDGE_TEMPERATURE_OVERRIDE），其他评委的温度设置保持不变。

启动方式：
    streamlit run app.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import closing
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable

import streamlit as st
from openai import OpenAI
from PyPDF2 import PdfReader

# --------------------------------------------------------------------------- #
# 本地历史记录（SQLite，不需要额外安装服务器）
# --------------------------------------------------------------------------- #
DEFAULT_DB_PATH = Path(__file__).resolve().with_name("interview.db")
DB_PATH = Path(os.environ.get("INTERVIEW_DB_PATH") or DEFAULT_DB_PATH).expanduser()


def _db_connect() -> sqlite3.Connection:
    """建立一个本地 SQLite 连接；数据库文件不存在时自动创建。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """创建 interviews 表（幂等，每次启动调用都安全）。"""
    with closing(_db_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job TEXT NOT NULL,
                company TEXT,
                mode TEXT,
                score REAL,
                questions_count INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_interviews_score ON interviews(score DESC)"
        )
        conn.commit()


def save_interview(
    job: str,
    company: str,
    mode: str,
    score: float | None,
    questions_count: int,
) -> int:
    """保存一条面试记录，返回自增 id。"""
    init_db()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(_db_connect()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO interviews (job, company, mode, score, questions_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job, company or "", mode, score, int(questions_count), created_at),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)


def load_interviews() -> list[dict[str, Any]]:
    """读取全部历史记录，按分数从高到低排序（没有分数的排最后）。"""
    init_db()
    with closing(_db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT id, job, company, mode, score, questions_count, created_at
            FROM interviews
            ORDER BY score DESC, created_at DESC, id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]

# --------------------------------------------------------------------------- #
# 全局深蓝科技风样式（纯 CSS，不引入外部框架）
# --------------------------------------------------------------------------- #
GLOBAL_CSS = """
<style>
:root {
  --navy-950: #0b0d10;
  --navy-800: #0b0d10;
  --ink: #0f172a;
  --accent: #4f7cff;
  --panel: #111418;
  --line: #23272f;
  --text: #e6e8eb;
  --muted: #8b929e;
}
html, body { background: var(--navy-950); }
[data-testid="stAppViewContainer"] {
  background-color: var(--navy-950);
  color: var(--text);
}
[data-testid="stHeader"], [data-testid="stToolbar"] { background: transparent; }
[data-testid="stMain"], [data-testid="stMainBlockContainer"] { background: transparent; }
[data-testid="stMainBlockContainer"] { max-width: 1180px; padding-top: 1.2rem; padding-bottom: 3rem; }
[data-testid="stSidebar"] {
  background: #0e1013;
  border-right: 1px solid var(--line);
}
[data-testid="stSidebar"] * { color: #c7ccd4; }
[data-testid="stSidebar"] [data-testid="stExpander"] {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
}
[data-testid="stSidebar"] [data-testid="stExpander"] summary {
  color: var(--text);
  font-weight: 600;
}
[data-testid="stMain"] h1, [data-testid="stMain"] h2,
[data-testid="stMain"] h3, [data-testid="stMain"] h4 {
  color: #f5f6f7 !important;
}
[data-testid="stMain"] p, [data-testid="stMain"] label, [data-testid="stMain"] li {
  color: #c7ccd4;
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {
  color: var(--muted) !important;
}
/* ---------- 回答 / 反问输入框：白底深黑字，保证对比度 ---------- */
[data-testid="stMain"] [data-testid="stTextArea"] [data-baseweb="textarea"] {
  background: #ffffff !important;
  border-color: #94a3b8 !important;
  border-radius: 12px !important;
}
[data-testid="stMain"] [data-testid="stTextArea"] textarea,
[data-testid="stMain"] [data-baseweb="textarea"] textarea,
[data-testid="stMain"] .stTextArea textarea {
  background: #ffffff !important;
  color: #1a1a1a !important;
  -webkit-text-fill-color: #1a1a1a !important;
  caret-color: #1a1a1a !important;
  font-size: 1rem !important;
  line-height: 1.7 !important;
  border-radius: 12px !important;
}
[data-testid="stMain"] [data-testid="stTextArea"] textarea:focus,
[data-testid="stMain"] [data-baseweb="textarea"] textarea:focus,
[data-testid="stMain"] .stTextArea textarea:focus {
  background: #ffffff !important;
  color: #1a1a1a !important;
  -webkit-text-fill-color: #1a1a1a !important;
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px rgba(37,99,235,.18) !important;
}
[data-testid="stMain"] [data-testid="stTextArea"] textarea::placeholder,
[data-testid="stMain"] [data-baseweb="textarea"] textarea::placeholder,
[data-testid="stMain"] .stTextArea textarea::placeholder {
  color: #6b7280 !important;
  -webkit-text-fill-color: #6b7280 !important;
  opacity: 1 !important;
}
[data-testid="stMain"] [data-testid="stTextArea"] textarea::selection,
[data-testid="stMain"] [data-baseweb="textarea"] textarea::selection,
[data-testid="stMain"] .stTextArea textarea::selection {
  background: #bfdbfe !important;
  color: #1a1a1a !important;
}
/* ---------- 面试页：左题目 / 右评分 ---------- */
.st-key-question_row { padding-top: .2rem; }
.st-key-question_row img,
.st-key-question_row [data-testid="stImage"] img {
  width: 64px !important;
  height: 64px !important;
  border-radius: 50% !important;
  object-fit: cover !important;
  border: 1px solid var(--line) !important;
}
.st-key-question_bubble {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 16px 18px;
}
.st-key-question_bubble p { color: var(--text); line-height: 1.7; }
.st-key-question_bubble [data-testid="stCaptionContainer"] p {
  color: var(--muted) !important;
  font-size: .76rem;
}
/* 右侧评委卡片 */
[class*="st-key-judge_card_"] {
  background: var(--panel) !important;
  border: 1px solid var(--line) !important;
  border-radius: 12px !important;
  padding: 10px 12px !important;
  margin-bottom: 8px !important;
}
[class*="st-key-judge_card_"] img,
[class*="st-key-judge_card_"] [data-testid="stImage"] img {
  width: 32px !important;
  height: 32px !important;
  border-radius: 50% !important;
  object-fit: cover !important;
  border: 1px solid var(--line) !important;
}
[class*="st-key-judge_card_"] [data-testid="stMarkdownContainer"] p {
  margin: .05rem 0 !important;
}
[class*="st-key-judge_card_"] [data-testid="stExpander"] {
  background: transparent !important;
  border: none !important;
  margin: 0 !important;
}
[class*="st-key-judge_card_"] [data-testid="stExpander"] summary {
  padding: .15rem 0 !important;
  font-size: .76rem !important;
  color: var(--muted) !important;
}
[class*="st-key-judge_card_"] [data-testid="stExpander"] table {
  margin: .35rem 0 .6rem !important;
}
.st-key-judge_summary {
  border-top: 1px solid var(--line);
  margin-top: 6px;
  padding-top: 10px;
}
.hero { text-align: center; padding: .8rem 0 1.2rem; }
.hero h1 {
  margin: 0;
  color: #f5f6f7 !important;
  font-size: clamp(2.2rem, 5vw, 3.2rem);
  line-height: 1.1;
  font-weight: 700;
  letter-spacing: .02em;
  text-shadow: none;
}
.hero .hero-subtitle {
  margin: .5rem 0 0;
  color: var(--muted) !important;
  font-size: .9rem;
  letter-spacing: .08em;
}
.hero .hero-rule {
  width: 48px;
  height: 1px;
  margin: .9rem auto 0;
  background: var(--line);
  opacity: 1;
}
.st-key-setup_card {
  box-sizing: border-box;
  width: 100%;
  max-width: 700px;
  margin: 0 auto;
  padding: 1.9rem 2rem 2rem;
  background: #fff;
  border: 1px solid rgba(148,163,184,.22);
  border-radius: 16px;
  box-shadow: 0 18px 50px rgba(0,0,0,.35);
}
.st-key-setup_card [data-testid="stForm"] {
  background: transparent !important;
  border: none !important;
  padding: 0 !important;
}
.st-key-setup_card label, .st-key-setup_card p,
.st-key-setup_card span, .st-key-setup_card div[data-testid="stMarkdownContainer"] * {
  color: var(--ink) !important;
}
.st-key-setup_card [data-testid="stCaptionContainer"] * {
  color: #64748b !important;
}
.st-key-setup_card [data-baseweb="input"],
.st-key-setup_card [data-baseweb="select"] > div {
  background: #f8fafc !important;
  border-color: #cbd5e1 !important;
  border-radius: 12px !important;
}
.st-key-setup_card input {
  color: var(--ink) !important;
  -webkit-text-fill-color: var(--ink) !important;
}
.st-key-setup_card [data-testid="stFileUploaderDropzone"] {
  background: #f8fafc !important;
  border: 1px dashed #93c5fd !important;
  border-radius: 14px !important;
}
.st-key-setup_card [data-testid="stFileUploaderDropzone"] * { color: #475569 !important; }
.st-key-quick_jobs { margin: .1rem 0 .25rem; }
.st-key-quick_jobs [data-testid="stHorizontalBlock"] { gap: .45rem; }
.st-key-quick_jobs button {
  background: #f1f5f9 !important;
  color: #475569 !important;
  border: 1px solid #e2e8f0 !important;
  border-radius: 999px !important;
  padding: .35rem .1rem !important;
  font-size: .76rem !important;
  font-weight: 600 !important;
  min-height: 2rem !important;
  white-space: nowrap !important;
  box-shadow: none !important;
}
.st-key-quick_jobs button * { color: #475569 !important; }
.st-key-quick_jobs button:hover {
  background: #e2e8f0 !important;
  color: #334155 !important;
  border-color: #cbd5e1 !important;
}
.st-key-quick_jobs button[kind="primary"],
.st-key-quick_jobs button[data-testid="stBaseButton-primary"] {
  background: var(--accent) !important;
  color: #fff !important;
  border-color: var(--accent) !important;
  box-shadow: none !important;
}
.st-key-quick_jobs button[kind="primary"] *,
.st-key-quick_jobs button[data-testid="stBaseButton-primary"] * {
  color: #fff !important;
}
.st-key-quick_jobs button[kind="primary"]:hover,
.st-key-quick_jobs button[data-testid="stBaseButton-primary"]:hover {
  background: #1d4ed8 !important;
  color: #fff !important;
  border-color: #1d4ed8 !important;
}
.st-key-setup_card [data-testid="stFormSubmitButton"] button,
.st-key-setup_card button[kind="primaryFormSubmit"],
.st-key-setup_card button[data-testid="stBaseButton-primaryFormSubmit"],
.st-key-setup_card button[type="submit"] {
  width: 100%;
  background: var(--accent) !important;
  color: #fff !important;
  border: none !important;
  border-radius: 12px !important;
  padding: .78rem 1rem !important;
  font-size: 1.02rem !important;
  font-weight: 700 !important;
  box-shadow: none !important;
}
.st-key-setup_card [data-testid="stFormSubmitButton"] button:hover,
.st-key-setup_card button[kind="primaryFormSubmit"]:hover,
.st-key-setup_card button[data-testid="stBaseButton-primaryFormSubmit"]:hover,
.st-key-setup_card button[type="submit"]:hover {
  background: #3f6ff0 !important;
  box-shadow: none !important;
}
.st-key-history_toggle button {
  background: var(--panel) !important;
  color: var(--text) !important;
  border: 1px solid var(--line) !important;
  border-radius: 8px !important;
  padding: .32rem .85rem !important;
  font-size: .82rem !important;
  font-weight: 600 !important;
  min-height: 2.1rem !important;
}
.st-key-history_toggle button:hover {
  background: #171b21 !important;
  border-color: #323844 !important;
}
[data-testid="stMain"] [data-testid="stForm"] {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
}
[data-testid="stMain"] [data-testid="stExpander"] {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
}
[data-testid="stMain"] [data-testid="stExpander"] summary { color: var(--text); }
[data-testid="stChatMessage"] {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
}
/* 历史题目气泡左侧的评委头像：48px 圆形裁剪 */
[data-testid="stChatMessage"] > img,
[data-testid="stChatMessage"] img[alt$=" avatar"] {
  box-sizing: border-box !important;
  width: 48px !important;
  height: 48px !important;
  flex-shrink: 0 !important;
  border-radius: 50% !important;
  object-fit: cover !important;
  border: 1px solid var(--line) !important;
}
[data-testid="stChatMessage"] * { color: var(--text) !important; }
[data-testid="stMain"] table { color: var(--text); }
[data-testid="stMain"] th {
  background: #1a1d23 !important;
  color: #d6dae1 !important;
  border-color: #2a2f37 !important;
}
[data-testid="stMain"] td { border-color: #2a2f37 !important; }
[data-testid="stMetric"] {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: .8rem 1rem;
}
[data-testid="stMetric"] * { color: var(--text) !important; }
[data-testid="stMain"] button[kind="primary"],
[data-testid="stMain"] button[data-testid="stBaseButton-primary"] {
  background: var(--accent) !important;
  border-color: var(--accent) !important;
  border-radius: 8px !important;
  box-shadow: none !important;
}
[data-testid="stMain"] button[kind="primary"]:hover,
[data-testid="stMain"] button[data-testid="stBaseButton-primary"]:hover {
  background: #3f6ff0 !important;
  border-color: #3f6ff0 !important;
}
/* 右侧评委卡片内的展开器保持极简，不继承外层卡片边框 */
[class*="st-key-judge_card_"] [data-testid="stExpander"],
[class*="st-key-judge_card_"] [data-testid="stExpander"] summary {
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
}
[class*="st-key-judge_card_"] [data-testid="stExpander"] summary {
  color: var(--muted) !important;
  font-size: .76rem !important;
  padding: .15rem 0 !important;
}
footer { visibility: hidden; }
</style>
"""


# --------------------------------------------------------------------------- #
# 6 位评委配置（写死在代码里）
# --------------------------------------------------------------------------- #
JUDGES: dict[str, dict[str, str]] = {
    "deepseek": {
        "api_key": st.secrets.get("DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", "")),
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    },
    "qwen": {
        "api_key": st.secrets.get("QWEN_API_KEY", os.environ.get("QWEN_API_KEY", "")),
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "glm": {
        "api_key": st.secrets.get("GLM_API_KEY", os.environ.get("GLM_API_KEY", "")),
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
    "kimi": {
        "api_key": st.secrets.get("KIMI_API_KEY", os.environ.get("KIMI_API_KEY", "")),
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.6",
    },
    "doubao": {
        "api_key": st.secrets.get("DOUBAO_API_KEY", os.environ.get("DOUBAO_API_KEY", "")),
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "ep-20261016182931-bhl88",
    },
    "wenxin": {
        "api_key": st.secrets.get("WENXIN_API_KEY", os.environ.get("WENXIN_API_KEY", "")),
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-5.1",
    },
}

# 评委展示名 / 人设 / 兜底接入点
#   endpoint_id：豆包（火山方舟）如果模型名不通，就把这里填成接入点 ID（ep-xxxxxx），
#                代码会自动用它重试一次并把可用的模型缓存下来。
JUDGE_META: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "persona": "你是有 10 年经验的大厂技术面试官，面过 200+ 候选人。",
        "endpoint_id": "",
    },
    "qwen": {
        "label": "通义千问",
        "persona": "你是 HR 总监，看人准，关注候选人稳定性和沟通能力。",
        "endpoint_id": "",
    },
    "glm": {
        "label": "智谱 GLM",
        "persona": "你是创业公司 CTO，看重能不能直接上手干活。",
        "endpoint_id": "",
    },
    "kimi": {
        "label": "Kimi",
        "persona": "你是压力面面试官，喜欢追问、挑刺。",
        "endpoint_id": "",
    },
    "doubao": {
        "label": "豆包",
        "persona": "你是面试官助理，负责记录和打分，客观中立。",
        "endpoint_id": "",
    },
    "wenxin": {
        "label": "文心一言",
        "persona": "你是资深技术面试官，做过多年线上系统，专挑方案漏洞、边界条件和线上事故。",
        "endpoint_id": "",
    },
}

JUDGE_ORDER: list[str] = list(JUDGES)

# --------------------------------------------------------------------------- #
# 评委头像（题目气泡左侧）
# --------------------------------------------------------------------------- #
APP_DIR = Path(__file__).resolve().parent
AVATARS: dict[str, str] = {
    "deepseek": "assets/deepseek.png",
    "qwen": "assets/qwen.png",
    "glm": "assets/glm.png",
    "kimi": "assets/kimi.png",
    "doubao": "assets/doubao.png",
    "wenxin": "assets/wenxin.png",
}
# 头像文件缺失 / 不可读时的默认灰色圆形占位图（内联 SVG，不依赖额外文件）。
DEFAULT_AVATAR = "data:image/svg+xml;base64," + base64.b64encode(
    b'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">'
    b'<circle cx="48" cy="48" r="48" fill="#9ca3af"/></svg>'
).decode("ascii")


def judge_avatar(judge: str) -> str:
    """返回评委头像；本地头像不存在时回退为灰色圆，不让页面报错。"""
    relative = AVATARS.get(judge or "")
    if relative:
        # 正常 streamlit run app.py 时用脚本所在目录；AppTest / 从其他目录启动时
        # 再兜底尝试当前工作目录，避免因为相对路径找不到头像。
        for base in (APP_DIR, Path.cwd()):
            path = base / relative
            if path.is_file():
                return str(path)
    return DEFAULT_AVATAR

# 评委级温度覆盖：写进这张表的评委，**所有环节**的调用都用这里的温度；
# 没写进表的评委沿用各环节本来设定的温度（出题 / 评审 / 打分 / 简历匹配度 = 1.0，
# 反问 = 0.8，综合点评 = 0.7，自检 = 0.0，输入纠错 = 0.3），行为完全不变。
# Kimi K2.6 关闭 thinking 后接口只接受 temperature=0.6。
JUDGE_TEMPERATURE_OVERRIDE: dict[str, float] = {
    "kimi": 0.6,
}

# 模型名兜底：主模型报「模型不存在 / 无权限 / 账户欠费不可用」时，按顺序再试这里的模型名。
# 豆包那种「换接入点 ID」的兜底仍走 JUDGE_META[...]["endpoint_id"]；
# 试通的模型会缓存下来，后续不再反复试错。
JUDGE_MODEL_FALLBACKS: dict[str, list[str]] = {
    # ernie-5.1 需要账户有可用余额 / 已开通；不可用时尝试降级到 ernie-5.0。
    "wenxin": ["ernie-5.0"],
}

# 评委的额外请求参数（走 OpenAI SDK 的 extra_body）。
# 文心 5.1 / 5.0 是推理模型，默认会先输出 reasoning_content 并计入 max_tokens；
# 本应用给评委的 max_tokens 只有 200/300，不关思考会导致正文为空，所以统一关闭。
# Kimi K2.6 同理：不关 thinking 时 300 token 全被 reasoning_content 吃掉，content 为空。
JUDGE_EXTRA_BODY: dict[str, dict[str, Any]] = {
    "wenxin": {"thinking": {"type": "disabled"}},
    "kimi": {"thinking": {"type": "disabled"}},
}

# --------------------------------------------------------------------------- #
# 规则常量
# --------------------------------------------------------------------------- #
MIN_QUESTIONS = 3          # 最少提问 3 题（不足 3 题不触发淘汰）
MAX_QUESTIONS = 8          # 最多提问 8 题（问满后进入反问环节）
MAX_REWRITES = 1           # 评审不通过时最多让反对的评委重写几次

# ---- 分层调用：3 个「快模型」负责出题，5 位评审负责投票，打分时 6 家全上（保质量）----
FAST_JUDGES: list[str] = ["glm", "wenxin", "kimi"]   # 主考官候选（3 个快模型）
QUICK_MODE_JUDGE = "glm"                              # 快速模式只用它一个模型
QUICK_MODE_LABEL = "快速模式（用小模型，快但简单）"
QUICK_MODE_HELP = (
    "勾选后只用 GLM 一个模型完成所有出题 + 打分，没有独立评审团，快但简单；"
    "不勾选则用分层架构（3 个快模型出题 + 5 位评审投票，6 位评委全量打分）。"
)
QUICK_JOB_OPTIONS: list[str] = [
    "前端",
    "Java 后端",
    "Python",
    "产品经理",
    "运营",
    "公务员",
]

REVIEW_JUDGES: list[str] = [*FAST_JUDGES, "deepseek", "qwen"]  # 5 位评审
REVIEW_TEAM_SIZE = len(REVIEW_JUDGES)  # 评审团人数（5 位）
REVIEW_PASS_VOTES = 3      # 5 位评审中 ≥3 票 pass=true，题目才发给用户

ELIMINATE_FALSE_VOTES = 2  # 6 位评委里 ≥2 位 continue=false，立即结束面试
EARLY_STOP_SCORE = 8.5     # 无人投 false 且最终平均分 ≥ 该分数时，可以提前收尾（不必凑满 8 题）

# ---- 速度相关 ----
QUESTION_MAX_TOKENS = 200  # 出题 / 重写 / 评审投票：一道题不需要长回答
SCORE_MAX_TOKENS = 300     # 打分：维度分 + comment + continue
REQUEST_TIMEOUT_SECONDS = 30  # 单次 API 调用超时秒数：超时就跳过该评委，不一直等

# ---- 打分维度（合计 10 分）----
SCORE_DIMENSIONS: dict[str, dict[str, Any]] = {
    "technical_depth": {
        "label": "技术深度",
        "max": 3,
        "desc": "专业原理、边界条件、方案取舍与技术深度",
    },
    "project_experience": {
        "label": "项目经验",
        "max": 3,
        "desc": "真实项目职责、数据结果、踩坑与复盘",
    },
    "communication": {
        "label": "沟通表达",
        "max": 2,
        "desc": "结构清晰、简洁、能让面试官听懂",
    },
    "logical_thinking": {
        "label": "逻辑思维",
        "max": 2,
        "desc": "因果关系、论证过程、边界条件与自洽性",
    },
}
DIMENSION_ORDER: list[str] = list(SCORE_DIMENSIONS)
SCORE_TOTAL_MAX = sum(meta["max"] for meta in SCORE_DIMENSIONS.values())

DIMENSION_ADVICE: dict[str, str] = {
    "project_experience": (
        "用 STAR 重写最弱的项目：背景 / 任务 / 你个人的动作 / 量化结果（QPS、延迟、转化率、成本至少补一个），"
        "并说清哪些是你做的、哪些是团队做的。"
    ),
    "communication": (
        "用「结论先行 + 三点式」重答：先一句话给结论，再列 3 个依据，最后收尾；录音回放，控制在 90 秒内。"
    ),
    "logical_thinking": (
        "先给结论，再给 2 条依据、1 个反例和边界条件，用「因为…所以…但是…」把因果链说完整。"
    ),
}

MODE_HR = "HR 面"
MODE_TECHNICAL = "技术面"
MODE_STRESS = "压力面"

SCALE_BIG = "大厂（BAT / 字节 / 美团级别）"
SCALE_MID = "中型公司"
SCALE_STARTUP = "创业公司"

RESUME_MAX_CHARS = 4000    # 简历文本截断长度，避免 prompt 过长
SINGLE_JUDGE_MAX_TOKENS = 700       # 其他单评委调用（简历匹配度 / 纠错等）
TEXT_JUDGE_MAX_TOKENS = 1500        # 反问回答 / 综合点评等长文本调用

# 真实性校验 + 输入纠错（开始面试前调 DeepSeek）
VERIFY_JUDGE = "deepseek"          # 固定用 DeepSeek 做真实性校验
VERIFY_MAX_TOKENS = 900            # 岗位 / 公司校验 + 候选推荐的 token 上限
VERIFY_ITEM_LIMIT = 3              # 岗位 / 公司各给 3 个最接近的真实候选
VERIFY_NONE_LABEL = "以上都不是，用我输入的"   # 输入确认有效时，允许坚持原输入
VERIFY_EMPTY_COMPANY = "（未填写）"            # 公司没填时给模型看的占位文本
VERIFY_VALID = "valid"             # 能确认真实存在
VERIFY_UNCERTAIN = "uncertain"     # 判断不出来（拼写错误 / 太模糊 / 小众）
VERIFY_INVALID = "invalid"         # 明显虚构 / 恶搞
VERIFY_EMPTY = "empty"             # 用户没填公司
VERIFY_FAIL_MESSAGE = "请输入真实的岗位和公司"
RESUME_INVALID_MESSAGE = "您上传的文件不是有效简历，请重新上传"
RESUME_CHECK_MAX_TOKENS = 300      # 简历有效性校验的 token 上限

# 兼容旧命名（外部脚本 / 旧测试可能引用）
SUGGEST_JUDGE = VERIFY_JUDGE
SUGGEST_MAX_TOKENS = VERIFY_MAX_TOKENS
SUGGEST_ITEM_LIMIT = VERIFY_ITEM_LIMIT
SUGGEST_NONE_LABEL = VERIFY_NONE_LABEL
SUGGEST_EMPTY_COMPANY = VERIFY_EMPTY_COMPANY

# 反问环节：候选人表示「没有其他问题」时的判定词
REVERSE_INTRO = "面试环节结束，你有什么要问我的吗？"
REVERSE_PROMPT_MARK = "【候选人的问题】"
REVERSE_END_EXACT = {
    "没有了", "没了", "没有啦", "没有", "无", "没有问题了", "没有问题", "没问题了", "没问题",
    "暂时没有", "暂时没有了", "暂无", "没有了谢谢", "谢谢没有了", "结束", "结束吧", "可以结束了",
    "end", "no", "nomore", "nothing", "nothingelse", "nothanks",
}
REVERSE_END_CONTAINS = (
    "没有了", "没有问题了", "没有其他问题", "没有其他", "没有别的", "暂时没有",
    "结束面试", "结束吧", "可以结束", "不想问了", "nomore", "nothingelse",
)

# 面试模式 -> 面试官/评委的关注点
INTERVIEW_MODES: dict[str, dict[str, str]] = {
    MODE_HR: {
        "desc": "气氛温和，聊动机、规划与软实力",
        "focus": "求职动机、职业规划、团队协作、沟通表达、稳定性与价值观",
        "style": (
            "本场是 HR 面：问题聚焦求职动机、职业规划、团队协作、沟通表达与稳定性，"
            "不考察硬核技术细节，语气温和但有分寸。"
        ),
    },
    MODE_TECHNICAL: {
        "desc": "专业深入，追问技术原理与项目细节",
        "focus": "技术原理、项目实现细节、方案取舍、性能与边界条件、线上踩坑经验",
        "style": (
            "本场是技术面：问题由浅入深，聚焦技术原理、项目实现细节、方案取舍、"
            "性能与边界条件、线上踩坑经验，会针对回答里的技术点继续追问。"
        ),
    },
    MODE_STRESS: {
        "desc": "高强度追问与质疑，考验抗压能力",
        "style": (
            "本场是压力面：语气强势、直接，会质疑候选人给出的数据与结论，"
            "追问「凭什么」「有没有证据」，刻意制造时间压力；但必须专业，"
            "只针对回答本身施压，绝不进行人身攻击、外貌评价或涉及歧视与隐私冒犯。"
        ),
        "focus": "抗压能力、逻辑自洽性、数据可信度、被质疑时的反应",
    },
}

# 公司规模 -> 出题深度 / 关注点 / 语气
COMPANY_SCALES: dict[str, dict[str, str]] = {
    SCALE_BIG: {
        "desc": "问题深、追问项目细节、压力感强",
        "style": (
            "这是一线大厂（BAT / 字节 / 美团这一档）：问题深、层层追问项目细节，"
            "要求候选人给出可验证的数据、量级与方案取舍理由，节奏快、压力感强。"
        ),
    },
    SCALE_MID: {
        "desc": "务实落地，看协作与稳定性",
        "style": (
            "这是一家成熟的中型公司：务实、关注落地能力与团队协作方式，"
            "问题难度中等，既看业务/技术细节也看沟通成本与稳定性，节奏平稳。"
        ),
    },
    SCALE_STARTUP: {
        "desc": "看重全能、能直接上手、薪资灵活",
        "style": (
            "这是一家创业公司：看重全能、能直接上手干活，"
            "关心「这件事你一个人能不能搞定」「多久能出结果」，对结果与主动性要求极高。"
        ),
    },
}

# 岗位方向 -> 出题范围 / 学习建议
# 关键词按顺序匹配：先匹配更具体的「全栈 / 前端 / Java 后端」，再落到通用后端 / 通用岗位。
JOB_TRACKS: dict[str, dict[str, Any]] = {
    "fullstack": {
        "key": "fullstack",
        "label": "全栈开发",
        "keywords": ["全栈", "full stack", "fullstack"],
        "focus": "前端基础（JS/CSS/框架）、后端语言与框架、数据库与缓存、接口设计、部署与项目经验",
        "advice": {
            "technical_depth": "按「前端 + 后端 + 数据库」三条线各补一个薄弱点：前端看 MDN 事件循环与 CSS 布局，后端看框架官方文档与事务，数据库看索引与慢查询。",
        },
    },
    "frontend": {
        "key": "frontend",
        "label": "前端",
        "keywords": [
            "前端", "frontend", "front-end", "javascript", "typescript",
            "react", "vue", "css", "web前端", "web 前端", "node.js", "nodejs",
        ],
        "focus": (
            "JavaScript（闭包 / 原型 / 事件循环 / 异步）、CSS（盒模型 / BFC / Flex / Grid / 响应式）、"
            "React 或 Vue（组件 / 状态管理 / 生命周期 / 性能）、前端性能优化（加载 / 渲染 / 缓存）、"
            "工程化（Webpack / Vite / CI / 代码规范）"
        ),
        "advice": {
            "technical_depth": (
                "重点补 JavaScript 闭包与事件循环（MDN「闭包」「事件循环」）、CSS BFC/Flex/Grid、"
                "React 官方文档「Thinking in React」、Vue 官方文档「深入响应式原理」、"
                "web.dev 性能优化指南与 Webpack/Vite 官方文档。"
            ),
        },
    },
    "java_backend": {
        "key": "java_backend",
        "label": "Java 后端",
        "keywords": ["java", "jvm", "spring", "mybatis", "微服务", "java开发", "java 开发"],
        "focus": (
            "Java 集合、JVM（内存模型 / GC / 类加载）、并发（线程池 / 锁 / JUC）、"
            "Spring（IoC / AOP / 事务 / Spring Boot）、MySQL（索引 / 事务 / 锁 / 优化）、"
            "Redis（缓存 / 数据结构 / 持久化 / 集群）、项目经验"
        ),
        "advice": {
            "technical_depth": (
                "重点补 JVM 内存模型（《深入理解 Java 虚拟机》第 2 章：Java 内存区域与内存溢出异常）、"
                "GC（同书第 3 章：垃圾收集器与内存分配策略）、并发（《Java 并发编程实战》线程池与锁）、"
                "Spring 事务传播（Spring 官方文档）、MySQL 索引与事务（《高性能 MySQL》）、"
                "Redis 缓存穿透 / 击穿 / 雪崩（Redis 官方文档）。"
            ),
        },
    },
    "mobile": {
        "key": "mobile",
        "label": "移动端",
        "keywords": ["android", "ios", "移动端", "客户端", "flutter", "react native"],
        "focus": "平台基础（生命周期 / 内存管理 / 线程模型）、UI 与交互、网络与缓存、性能优化、项目经验",
        "advice": {
            "technical_depth": "按平台补官方文档：Android 开发者文档（生命周期 / 内存 / 性能）、iOS Human Interface Guidelines；再补网络缓存与启动优化。",
        },
    },
    "algorithm_data": {
        "key": "algorithm_data",
        "label": "算法 / 数据",
        "keywords": ["算法", "数据", "机器学习", "深度学习", "大模型", "ai", "推荐", "nlp", "cv"],
        "focus": "数学与统计基础、机器学习 / 深度学习原理、特征工程、模型评估、SQL / 数据处理、项目与业务指标",
        "advice": {
            "technical_depth": "按方向补基础：机器学习看《统计学习方法》对应章节，深度学习看《深度学习》相关章节；再用一个真实数据集做完整特征工程 + 模型评估。",
        },
    },
    "testing": {
        "key": "testing",
        "label": "测试",
        "keywords": ["测试", "qa", "自动化测试", "测试开发", "质量"],
        "focus": "测试用例设计、接口 / UI 自动化、性能测试、缺陷定位、质量度量、项目经验",
        "advice": {
            "technical_depth": "补《软件测试的艺术》用例设计、测试金字塔、接口自动化（Pytest/Postman）与性能测试（JMeter/k6）官方文档。",
        },
    },
    "ops": {
        "key": "ops",
        "label": "运维 / SRE",
        "keywords": ["运维", "sre", "devops", "云原生", "kubernetes", "k8s"],
        "focus": "Linux、网络、Shell / Python、容器与 Kubernetes、CI/CD、监控告警、故障排查与项目经验",
        "advice": {
            "technical_depth": "补 Kubernetes 官方文档、Prometheus 监控、Linux 性能排查（《性能之巅》对应章节），并做一次完整的部署 + 监控 + 故障演练。",
        },
    },
    "product": {
        "key": "product",
        "label": "产品 / 运营",
        "keywords": ["产品", "运营", "市场", "增长", "pm"],
        "focus": "需求分析、用户研究、竞品分析、数据指标、项目推进、沟通协作与复盘",
        "advice": {
            "technical_depth": "补需求分析与竞品拆解方法（《人人都是产品经理》）、指标体系（AARRR / 留存 / 转化），并完整复盘一个你推动过的项目。",
        },
    },
    "backend_generic": {
        "key": "backend_generic",
        "label": "后端开发",
        "keywords": ["后端", "服务端", "backend", "server", "python"],
        "focus": "语言基础、数据库（索引 / 事务 / 锁）、缓存、消息队列、接口与系统设计、项目经验",
        "advice": {
            "technical_depth": "补数据库索引与事务（《数据库系统概念》对应章节）、Redis 缓存、消息队列与系统设计基础，并做一次接口性能压测。",
        },
    },
    "generic": {
        "key": "generic",
        "label": "通用岗位",
        "keywords": [],
        "focus": "围绕该岗位 JD 的真实工作内容：核心职责、必备技能、项目经验、协作与复盘",
        "advice": {
            "technical_depth": "把岗位 JD 拆成 3 个高频能力点，每个点找一份官方文档或经典教材对应章节，再做一个小项目验证。",
        },
    },
}

# 针对具体题目关键词的学习建议（比只按岗位给建议更具体）
JOB_TRACK_TOPIC_ADVICE: dict[str, list[tuple[tuple[str, ...], str]]] = {
    "java_backend": [
        (("jvm", "内存模型", "gc", "垃圾回收", "类加载"),
         "JVM 内存模型 / GC 这块答得不清楚，建议看《深入理解 Java 虚拟机》第 2 章（Java 内存区域与内存溢出异常）和第 3 章（垃圾收集器与内存分配策略）。"),
        (("并发", "线程", "线程池", "锁", "juc"),
         "并发这块答得不够扎实，建议看《Java 并发编程实战》的线程池、锁与 JUC 工具类章节，并用 JMH 做一次压测。"),
        (("spring", "aop", "ioc", "bean", "@transactional"),
         "Spring 这块建议对照 Spring 官方文档补 IoC / AOP / 事务传播，重点说清 @Transactional 在什么情况下会失效。"),
        (("mysql", "索引", "慢查询", "sql", "事务"),
         "MySQL 这块建议看《高性能 MySQL》索引与事务章节，并用 EXPLAIN 分析一条真实慢查询。"),
        (("redis", "缓存", "穿透", "击穿", "雪崩"),
         "Redis 这块建议看 Redis 官方文档的缓存与持久化章节，重点补缓存穿透 / 击穿 / 雪崩的处理方案。"),
    ],
    "frontend": [
        (("闭包", "作用域"),
         "JS 闭包这块答得不清楚，建议看 MDN「闭包」并手写 3 个闭包场景（防抖、节流、私有变量）。"),
        (("事件循环", "event loop", "异步", "promise", "宏任务", "微任务"),
         "事件循环 / 异步这块建议看 MDN「事件循环」与「Promise」，手画一遍宏任务 / 微任务执行顺序。"),
        (("css", "布局", "bfc", "flex", "grid", "响应式"),
         "CSS 布局这块建议看 MDN BFC / Flex / Grid 文档，并手写一个三栏响应式布局。"),
        (("react", "hooks", "组件", "状态管理"),
         "React 这块建议看 React 官方文档「Thinking in React」与 Hooks 章节，补组件拆分、状态提升和重渲染优化。"),
        (("vue", "响应式", "computed", "watch"),
         "Vue 这块建议看 Vue 官方文档「深入响应式原理」，说清 reactive / ref / computed / watch 的差异。"),
        (("性能", "lcp", "首屏", "包体积", "缓存"),
         "前端性能这块建议看 web.dev 的 Core Web Vitals 指南，并用 Lighthouse 对真实页面做一次优化前后对比。"),
        (("工程化", "webpack", "vite", "ci", "规范"),
         "工程化这块建议看 Webpack / Vite 官方文档，补构建流程、代码分割、缓存与 CI 质量门禁。"),
    ],
}

# --------------------------------------------------------------------------- #
# Prompt 模板（统一用 str.format 填充：模板里的 json 裸花括号写成 {{ }}）
# --------------------------------------------------------------------------- #
def detect_job_track(job: str) -> dict[str, Any]:
    """按岗位名称识别出题方向；识别不到时返回「通用岗位」。"""
    text = (job or "").strip().lower()
    for track in JOB_TRACKS.values():
        if any(keyword.lower() in text for keyword in track["keywords"]):
            return track
    return JOB_TRACKS["generic"]


WRITER_SYSTEM_TEMPLATE = """你是「{label}」评委，本场面试的**主考官**，负责出第 {index} 道题。
{persona}

{context}

# 出题要求
1. 必须贴合应聘岗位与该公司的招聘口味，一次只问一个问题
2. 必须落在下面「本岗位出题范围」内，不要问与该岗位方向无关的题
3. 本场尽量覆盖不同子主题，不要连续两题问同一个知识点
4. 口语化，像面试官当场开口问的，一般不超过 80 字
5. 要有追问价值：考察具体经历、数据、方案取舍或边界条件，不要问空泛套话
6. 严禁与「已经问过的问题」重复或高度相似
7. 只输出一个 json 对象，不要任何解释、不要 Markdown 代码块标记
{{"question": "你要问的问题"}}"""

WRITER_USER_TEMPLATE = """这是第 {index} 道题（本场共 {min_q}-{max_q} 题，由评委动态决定）。

# 已经问过的问题
{asked}

请给出第 {index} 道题（只输出 json）。"""

REVIEW_SYSTEM_TEMPLATE = """这是一道模拟面试题，请判断它是否像真实面试官会问的。标准放宽：只要题目方向对、不离谱、和岗位相关就通过，不要吹毛求疵。返回 JSON：{{"pass": true, "reason": "简短理由"}}（不通过时把 true 改成 false）

{context}"""

REVIEW_USER_TEMPLATE = """# 评审轮次
第 {round} 轮（题目来源：{kind}）

# 已经问过的问题
{asked}

# 待评审的题目
{question}

请给出你的评审结论（只输出 json）。"""

REWRITE_SYSTEM_TEMPLATE = """你是「{label}」评委，刚才在评审团里投了「不通过」。现在请你亲自重写这道题。
{persona}

{context}

# 重写要求
1. 必须针对评审意见指出的问题改：跑题→拉回「本岗位出题范围」，套话→换成具体场景，重复→换个考察点
2. 一次只问一个问题，口语化，不超过 80 字
3. 不要与「已经问过的问题」重复
4. 只输出一个 json 对象，不要任何解释、不要 Markdown 代码块标记
{{"question": "重写后的问题"}}"""

REWRITE_USER_TEMPLATE = """# 第 {index} 道题待重写

# 原题（评审不通过）
{question}

# 评审团的意见
{reasons}

# 已经问过的问题
{asked}

请给出你重写后的题目（只输出 json）。"""

SCORE_SYSTEM_TEMPLATE = """你是「{label}」评委，现在要给候选人的回答分维度打分。
{persona}

{context}

# 打分维度（四项相加就是本题总分，满分 10 分）
1. technical_depth 技术深度（0-3 分）：专业原理是否准确、边界条件是否说清、方案取舍是否有依据
2. project_experience 项目经验（0-3 分）：是否讲清真实项目职责、量化结果、踩坑与复盘
3. communication 沟通表达（0-2 分）：结构是否清晰、是否简洁、面试官是否容易听懂
4. logical_thinking 逻辑思维（0-2 分）：因果关系、论证过程、反例与边界条件是否自洽

# 打分要求
1. 四个维度都必须给整数分，严格按该岗位的真实招聘标准，不给同情分、不给人情分
2. 不要额外输出 score；系统会把四项相加作为本题得分
3. comment：直接、不客气的点评——答不到点上就说「完全没答到点上」，数据站不住就说「这个数字没有支撑」；
   只评价回答本身，绝不进行人身攻击、外貌评价或涉及歧视与隐私冒犯
4. continue：true = 可以继续面试；false = 你认为该候选人与岗位明显不匹配、应当立即结束面试；
   回答有瑕疵但方向正确的，请给 true
5. 只输出一个 json 对象，不要解释、不要 Markdown 代码块标记：
{{"technical_depth": 2, "project_experience": 2, "communication": 1, "logical_thinking": 1, "comment": "直接不客气的点评", "continue": true}}"""

SCORE_USER_TEMPLATE = """# 第 {index} 道题（主考官：{writer_label}）
{question}

# 候选人的回答
{answer}

请给出你的评分（只输出 json）。"""

REVERSE_ANSWER_PROMPT = """面试问答环节已经结束，现在进入反问环节。候选人会向你提问，请你继续以{target}的面试官身份自然作答。

回答要求：
1. 直接回答，不要回避，语气要像真实的{scale}面试官（可以用「我们这边」这样的口吻）
2. 可以聊薪资构成（base / 年终 / 奖金 / 期权）、加班与工作强度、团队氛围与汇报关系、晋升与成长路径、
   业务现状与挑战、技术栈与协作方式；不清楚的地方坦率说明
3. 谈薪资给区间与结构，不承诺 offer、不做绝对化承诺：大厂强调定级与结构性收入，创业公司强调弹性与期权
4. 中文口语化，控制在 150 字以内；不要输出 json、不要使用 Markdown 标题
5. 与公司或岗位完全无关的问题，礼貌说明并引导回正题"""

RESUME_MATCH_PROMPT = """请根据上面的面试问答内容与候选人的简历，评估候选人与应聘岗位「{job}」的匹配度。

只输出一个 json 对象，不要任何解释或 Markdown 代码块标记，字段名必须完全一致：
{{"resume_match_score": 85, "resume_match_comment": "一句话说明匹配与不匹配的关键点"}}

要求：
- resume_match_score：0-100 的整数，综合岗位要求、项目经历、技能栈、年限与 {judge_count} 位评委的现场打分
- resume_match_comment：不超过 60 字，客观具体，不要空泛套话"""

VERIFY_SYSTEM_PROMPT = """你是招聘信息真实性校验助手：用户可能用错别字、拼音、英文缩写、简称，
甚至填完全虚构的岗位或公司。你要在纠正输入的同时判断它们是否真实存在，只输出 json 对象，不要解释。"""

VERIFY_USER_TEMPLATE = """用户输入了岗位：{job}，公司：{company}。请判断用户可能想表达什么岗位方向和什么公司，并校验真实性，返回 JSON：{{"job": {{"status": "valid", "reason": "常见的真实招聘岗位", "suggestions": ["Java 后端开发工程师", "后端开发工程师", "服务端开发工程师"]}}, "company": {{"status": "valid", "reason": "真实存在的大型互联网公司", "suggestions": ["字节跳动", "阿里巴巴", "腾讯"]}}}}

判定标准：
1. status 只能是 valid / uncertain / invalid：
   - valid：能确认是真实存在的招聘岗位 / 真实注册的公司（例如「Java 后端开发」「字节跳动」）
   - uncertain：信息太模糊、拼写错误、缩写、或不了解的小众公司，无法确认（例如「java 后 Duan」「jool」）
   - invalid：明显虚构、恶搞、不可能真实存在（例如「睡觉专家」「宇宙无敌公司」）
2. suggestions：job / company 各给 3 个真实存在的候选，按可能性从高到低排序；
   status 不是 valid 时，suggestions 里不要重复用户原文，必须是真实存在的岗位 / 公司
3. 公司没填（{empty}）时：company.status = "empty"，suggestions = []
4. reason：一句话说明判断依据，不要空泛套话
5. 只输出 json 对象，不要 Markdown 代码块标记、不要多余文字"""

RESUME_CHECK_SYSTEM_PROMPT = """【简历有效性校验】你是简历格式校验助手。你只判断一段文字是不是一份有效简历，
不评价候选人是否优秀，也不核验简历内容的真假。只输出 json 对象，不要解释。"""

RESUME_CHECK_USER_TEMPLATE = """以下是从用户上传的 PDF 中解析出的文字：

<文件内容>
{text}
</文件内容>

请判断它是不是一份有效简历。有效简历通常包含姓名 / 联系方式、教育经历、工作或项目经历、技能等要素中的多数；
风景图说明、乱码、小说、论文、菜单、合同、广告等都不是简历。

只输出 JSON：{{"is_resume": true, "reason": "一句话说明依据"}}
- 是简历：is_resume = true
- 不是简历：is_resume = false
- 无法判断：is_resume = null"""

SUMMARY_SYSTEM_PROMPT = """你是本场模拟面试的**招聘委员会主审**。{judge_count} 位评委已经各自打完分，
请根据下面给出的客观数据写一份中文面试总结报告（Markdown 格式）。"""

SUMMARY_USER_TEMPLATE = """## 本场客观数据（必须如实引用，不要改动数字）
{facts}

## 逐题评委记录
{records}

# 报告要求
1. 必须包含这些部分：面试结论（通过 / 待定 / 不通过，并注明是否提前淘汰）、最终平均分与打分依据、
   分维度得分（技术深度 / 项目经验 / 沟通表达 / 逻辑思维）与哪块最弱、
   各题得分与评委分歧（谁最严、谁最宽）、简历匹配度（有简历时）、候选人的优势、主要不足、改进建议
2. 点评直接、不委婉：该说「完全没答到点上」「这个数据站不住」「逻辑是散的」就直说，
   禁止用「建议可以再加强一下」「还需继续努力」这类废话
3. 「改进建议」必须写 3-5 条，每条都要同时满足：
   - 点出具体是哪一道题 / 哪个知识点答得不好（引用候选人的原话或题目关键词）
   - 给出具体学习材料：书名 + 章节，或官方文档 + 章节，或一个可以动手做的练习
   - 给出一句可以直接照说的示范回答
   示例：「你 JVM 内存模型这块答得不清楚（第 2 题），建议看《深入理解 Java 虚拟机》第 2 章，
   并用一句话说清堆 / 栈 / 方法区 / 程序计数器分别放什么。」
4. 引用候选人回答里的实际内容，不要空泛套话；优先围绕「维度短板排序」里最弱的 1-2 个维度写建议"""

# --------------------------------------------------------------------------- #
# session_state 管理
# --------------------------------------------------------------------------- #
def default_state() -> dict[str, Any]:
    """会话初始状态。stage 取值为 setup / confirm（输入纠错确认）/ interview / report。"""
    return {
        "stage": "setup",
        "phase": "questions",       # questions（提问）/ reverse（反问）/ finished（问答结束）
        "job": "",                  # 应聘岗位
        "mode": MODE_HR,            # 面试模式
        "scale": SCALE_BIG,         # 公司规模
        "quick_mode": False,        # 是否勾选「快速模式」（只用一个小模型出题 + 打分）
        "company": "",              # 目标企业名称（选填）
        "resume_name": "",          # 简历文件名
        "resume_text": "",          # 简历解析出的纯文本（评委们的「简历参考」）
        "qa_history": [],           # 逐题记录（含主考官 / 评审投票 / 6 位评委会分数）
        "reverse_history": [],      # 反问环节 [{"question", "answer", "judge"}]
        "current_question": "",     # 当前等待回答的题目
        "current_question_meta": {},  # 当前题目的出题信息（主考官 / 评审轮次）
        "awaiting_answer": False,
        "eliminated": False,        # 是否被提前淘汰（≥2 位评委 continue=false）
        "eliminate_reason": "",
        "closed_by": "",            # 问答结束原因：eliminated / high_score / cap / manual
        "resume_match": None,       # {"score": int, "comment": str}
        "report": "",               # AI 综合点评（Markdown）
        "report_error": "",
        "error": "",                # 最近一次错误提示
        "judge_health": {},         # 6 位评委连通性自检结果
        "judge_models": {},         # 每个评委当前生效的模型（跨 rerun 复用，避免反复试错）
        "confirm_end": False,       # 提前结束的二次确认
        "pending_setup": {},        # 表单已提交、待确认的本场配置（纠错页确认后才写进 job / company）
        "suggest": {},              # 输入纠错的候选 {"jobs": [...], "companies": [...]}
        "suggest_error": "",        # 输入纠错调用失败的错误原文（不影响开始面试）
        "input_check": {},          # 真实性校验结果 {"job": {...}, "company": {...}}
        "resume_check": {},         # 简历有效性校验结果 {"is_resume": bool, "reason": str}
        "history_saved": False,     # 本场是否已写入 SQLite 历史记录
        "history_record_id": 0,     # 写入后的记录 id
        "history_error": "",        # 历史记录保存失败提示
        "show_history": False,      # 是否展开右上角「历史记录」
    }


def init_session_state() -> None:
    for key, value in default_state().items():
        st.session_state.setdefault(key, value)


def reset_session() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    init_session_state()


# --------------------------------------------------------------------------- #
# 简历解析（PyPDF2）
# --------------------------------------------------------------------------- #
class ResumeParseError(Exception):
    """简历 PDF 解析失败（加密、损坏、空文件等）。"""


def extract_pdf_text(data: bytes) -> str:
    """用 PyPDF2 抽取 PDF 简历的纯文本，失败抛出 ResumeParseError。

    解析出的文本会做空白归一化并截断到 RESUME_MAX_CHARS，避免 prompt 过长。
    """
    if not data:
        raise ResumeParseError("文件内容为空")
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            try:
                decrypted = bool(reader.decrypt(""))  # 先试试空密码
            except Exception:  # noqa: BLE001 —— 不同版本行为不一致，统一按「打不开」处理
                decrypted = False
            if not decrypted:
                raise ResumeParseError("PDF 已加密，无法读取文字")
        chunks = [page.extract_text() or "" for page in reader.pages]
    except ResumeParseError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 损坏 / 非 PDF 等统一转成友好提示
        raise ResumeParseError(f"无法解析该 PDF（{exc}）") from exc

    text = "\n".join(chunks)
    text = re.sub(r"[\u3000 \t]+", " ", text)       # 合并空格
    text = re.sub(r"\n{3,}", "\n\n", text).strip()  # 折叠多余空行
    return text[:RESUME_MAX_CHARS]


# --------------------------------------------------------------------------- #
# 6 家模型的并发调用层（concurrent.futures）
# --------------------------------------------------------------------------- #
_WORKING_MODEL: dict[str, str] = {}      # 每个评委当前可用的模型名 / 接入点 ID
_WORKING_MODEL_LOCK = threading.Lock()


@st.cache_resource(show_spinner=False)
def get_clients() -> dict[str, OpenAI]:
    """为 6 位评委各建一个 OpenAI 兼容客户端（在主线程创建，可安全用于子线程）。

    客户端统一带 30 秒超时、且不做自动重试：超时 / 失败快速返回，
    上层按「跳过这位评委」处理，不会一直等。
    """
    return {
        name: OpenAI(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
        for name, cfg in JUDGES.items()
    }


def sync_working_models_to_state() -> None:
    """把「当前可用模型」写入 session_state，跨 rerun 复用（避免每次都试错）。"""
    try:
        saved = dict(st.session_state.get("judge_models") or {})
        with _WORKING_MODEL_LOCK:
            saved.update(_WORKING_MODEL)
        st.session_state["judge_models"] = saved
    except Exception:  # noqa: BLE001 —— 没有 Streamlit 上下文时（例如单测）忽略
        pass


def hydrate_working_models() -> None:
    """从 session_state 恢复上次试出来的可用模型。"""
    try:
        saved = st.session_state.get("judge_models") or {}
    except Exception:  # noqa: BLE001 —— 同上
        return
    with _WORKING_MODEL_LOCK:
        for name, model in saved.items():
            if name in JUDGES and model:
                _WORKING_MODEL.setdefault(name, model)


def judge_models(judge: str) -> list[str]:
    """该评委要尝试的模型顺序：主模型 → 模型名兜底（如文心） → 接入点 ID 兜底（豆包）。"""
    cfg = JUDGES[judge]
    with _WORKING_MODEL_LOCK:
        cached = _WORKING_MODEL.get(judge)
    if cached:
        return [cached]
    candidates = [cfg["model"]]
    for fallback in JUDGE_MODEL_FALLBACKS.get(judge, []):
        if fallback and fallback not in candidates:
            candidates.append(fallback)
    endpoint = (JUDGE_META.get(judge, {}).get("endpoint_id") or "").strip()
    if endpoint and endpoint not in candidates:
        candidates.append(endpoint)
    return candidates


def current_model(judge: str) -> str:
    """该评委当前生效的模型（界面展示用）。"""
    with _WORKING_MODEL_LOCK:
        return _WORKING_MODEL.get(judge) or JUDGES[judge]["model"]


def looks_like_model_error(message: str) -> bool:
    """粗判错误是否「换一个模型就能解决」，以决定要不要走模型兜底。

    除了模型名 / 接入点错误，千帆的 account_overdue（该模型欠费 / 未开通）也属于
    可以尝试下一个模型的场景。
    """
    lowered = message.lower()
    return any(
        key in lowered
        for key in (
            "model",
            "endpoint",
            "not found",
            "does not exist",
            "no such",
            "invalid",
            "404",
            "overdue",
        )
    )


def judge_temperature(judge: str, stage_temperature: float) -> float:
    """算出该评委这次调用实际要用的温度。

    命中 JUDGE_TEMPERATURE_OVERRIDE 的评委（Kimi）一律用覆盖值；其他评委沿用
    调用方按环节传入的温度，和改动前完全一致。
    """
    return JUDGE_TEMPERATURE_OVERRIDE.get(judge, stage_temperature)


def is_timeout_error(exc: BaseException) -> bool:
    """判断异常是不是「超时」（openai 抛 APITimeoutError，底层多为 httpx 超时）。"""
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "timeout" in name or "timed out" in text or "timeout" in text


def model_hint(judge: str, error: str) -> str:
    """给「模型名 / 接入点 / 账户欠费」相关的错误补上可操作的提示。"""
    if judge == "doubao" and not (JUDGE_META["doubao"].get("endpoint_id") or "").strip():
        error += (
            "；提示：豆包模型名可能不对，请把 JUDGE_META['doubao']['endpoint_id'] "
            "填成火山方舟的接入点 ID（ep-xxx）"
        )
    if "overdue" in error.lower():
        error += (
            "；提示：千帆返回 account_overdue（账户欠费 / 余额不足 / 该模型未开通），"
            "请到千帆控制台充值或确认 ernie-5.1 已开通；代码已自动尝试降级到 ernie-5.0。"
        )
        return error
    fallbacks = JUDGE_MODEL_FALLBACKS.get(judge) or []
    if fallbacks and looks_like_model_error(error):
        tried = " / ".join([JUDGES[judge]["model"], *fallbacks])
        error += (
            f"；提示：{JUDGE_META[judge]['label']} 已按顺序试过 {tried}，"
            "都不通就在千帆控制台换一个已开通的模型名（改 JUDGE_MODEL_FALLBACKS）"
        )
    return error


def judge_kwargs(
    judge: str,
    messages: list[dict[str, str]],
    *,
    json_mode: bool,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    """统一的请求参数（含 30 秒超时；超时由调用方按「跳过该评委」处理）。"""
    kwargs: dict[str, Any] = {
        "messages": messages,
        "temperature": judge_temperature(judge, temperature),
        "max_tokens": max_tokens,
        "timeout": REQUEST_TIMEOUT_SECONDS,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    extra_body = JUDGE_EXTRA_BODY.get(judge)
    if extra_body:
        kwargs["extra_body"] = dict(extra_body)
    return kwargs


def ask_judge(
    client: OpenAI,
    judge: str,
    messages: list[dict[str, str]],
    *,
    json_mode: bool = True,
    temperature: float = 1.0,
    max_tokens: int = SINGLE_JUDGE_MAX_TOKENS,
) -> tuple[str, str]:
    """给单个评委发一次请求（线程安全，可在子线程执行），返回 (内容, 错误)。

    正常路径只发一次请求；只有「换一个模型可能解决」的错误（模型名 / 接入点 /
    千帆 account_overdue 等）才会按 judge_models() 的顺序换备选模型
    （如文心 ernie-5.1 → ernie-5.0）或换接入点 ID 重试。
    每次调用 REQUEST_TIMEOUT_SECONDS（默认 30）秒超时：超时直接跳过这位评委，
    不重试、不一直等。注意：这里必须用普通字符串做 docstring——f-string 会被
    Streamlit 的 magic 当成表达式写进页面，在子线程里触发无意义的告警。
    """
    label = JUDGE_META[judge]["label"]
    kwargs = judge_kwargs(
        judge, messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens
    )
    kwargs["stream"] = False

    last_error = ""
    for model in judge_models(judge):
        try:
            response = client.chat.completions.create(model=model, **kwargs)
            with _WORKING_MODEL_LOCK:
                _WORKING_MODEL[judge] = model
            return response.choices[0].message.content or "", ""
        except Exception as exc:  # noqa: BLE001 —— 单评委失败不影响其他评委
            if is_timeout_error(exc):
                return "", f"{label} 超时（>{REQUEST_TIMEOUT_SECONDS}s），已跳过"
            last_error = f"{label} 调用失败（{model}）：{exc}"
            if not looks_like_model_error(last_error):
                break
    return "", model_hint(judge, last_error)


def _stream_one_model(
    client: OpenAI, model: str, kwargs: dict[str, Any], on_chunk: Callable[[str], None]
) -> tuple[str, str, bool]:
    """流式请求一个模型，返回 (文本, 错误, 是否超时)；模型不支持流式时退回一次性请求。"""
    parts: list[str] = []
    try:
        stream = client.chat.completions.create(model=model, stream=True, **kwargs)
        for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            delta = getattr(choices[0], "delta", None) if choices else None
            piece = getattr(delta, "content", "") if delta is not None else ""
            if piece:
                parts.append(piece)
                on_chunk("".join(parts))
        return "".join(parts), "", False
    except Exception as exc:  # noqa: BLE001 —— 单评委失败不影响其他评委
        if parts:
            # 页面上已经显示过部分内容：不再重试，避免重复输出
            return "", str(exc), is_timeout_error(exc)
        try:  # 有些兼容接口不支持 stream=true：退回一次性请求再试一次
            kwargs.pop("stream", None)
            response = client.chat.completions.create(model=model, stream=False, **kwargs)
            text = response.choices[0].message.content or ""
            if text:
                on_chunk(text)
            return text, "", False
        except Exception as retry_exc:  # noqa: BLE001
            return "", str(retry_exc), is_timeout_error(retry_exc)


def stream_judge(
    client: OpenAI,
    judge: str,
    messages: list[dict[str, str]],
    on_chunk: Callable[[str], None],
    *,
    json_mode: bool = True,
    temperature: float = 1.0,
    max_tokens: int = SCORE_MAX_TOKENS,
) -> tuple[str, str]:
    """流式调用单个评委：每来一段增量文本就回调 on_chunk(全文)（用于逐字显示）。

    规则与 ask_judge 一致：只有模型名 / 接入点相关的错误才换下一个候选，
    超时直接跳过；某个模型不支持流式时自动退回一次性请求。返回 (完整文本, 错误)。
    """
    label = JUDGE_META[judge]["label"]
    kwargs = judge_kwargs(
        judge, messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens
    )
    last_error = ""
    for model in judge_models(judge):
        text, err, timed_out = _stream_one_model(client, model, kwargs, on_chunk)
        if not err:
            with _WORKING_MODEL_LOCK:
                _WORKING_MODEL[judge] = model
            return text, ""
        if timed_out:
            return text, f"{label} 超时（>{REQUEST_TIMEOUT_SECONDS}s），已跳过"
        last_error = f"{label} 调用失败（{model}）：{err}"
        if not looks_like_model_error(last_error):
            break
    return "", model_hint(judge, last_error)


def call_many(
    names: Iterable[str],
    build_messages: Callable[[str], list[dict[str, str]]],
    *,
    json_mode: bool = True,
    temperature: float = 1.0,
    max_tokens: int = SINGLE_JUDGE_MAX_TOKENS,
) -> dict[str, tuple[str, str]]:
    """并发调用多个评委，返回 {评委: (内容, 错误)}；单个评委失败不影响其他评委。

    temperature 是「本环节」的温度；若某位评委在 JUDGE_TEMPERATURE_OVERRIDE 里
    （当前只有 Kimi），该评委改用覆盖值，其他评委仍是这里的值。

    prompt 在主线程里先全部拼好，子线程只做网络请求（不触碰 Streamlit API）。
    """
    judge_names = list(names)
    if not judge_names:
        return {}
    hydrate_working_models()
    clients = get_clients()
    payloads = {name: build_messages(name) for name in judge_names}
    results: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=len(judge_names), thread_name_prefix="judge") as pool:
        futures = {
            pool.submit(
                ask_judge,
                clients[name],
                name,
                payloads[name],
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
            ): name
            for name in judge_names
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 —— 兜底，避免子线程异常影响整轮
                results[name] = ("", f"{JUDGE_META[name]['label']} 调用异常：{exc}")
    sync_working_models_to_state()
    return results


def call_one(
    judge: str,
    messages: list[dict[str, str]],
    *,
    json_mode: bool = True,
    temperature: float = 1.0,
    max_tokens: int = SINGLE_JUDGE_MAX_TOKENS,
) -> tuple[str, str]:
    """单评委调用（出题 / 反问回答 / 综合点评这类只需要一位评委的请求）。"""
    result = call_many(
        [judge],
        lambda _name: messages,
        json_mode=json_mode,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return result.get(judge, ("", "未知错误"))


def call_with_fallback(
    judges: Iterable[str],
    build_messages: Callable[[str], list[dict[str, str]]],
    *,
    json_mode: bool = True,
    temperature: float = 1.0,
    max_tokens: int = SINGLE_JUDGE_MAX_TOKENS,
) -> tuple[str, str, str]:
    """按顺序尝试多位评委，返回 (实际响应的评委, 内容, 错误)。前一位失败才换下一位。"""
    last_error = ""
    for name in judges:
        text, err = call_one(
            name,
            build_messages(name),
            json_mode=json_mode,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if not err and text.strip():
            return name, text, ""
        last_error = err or f"{JUDGE_META[name]['label']} 没有返回内容。"
    return "", "", last_error


# --------------------------------------------------------------------------- #
# 返回值解析
# --------------------------------------------------------------------------- #
def load_json_object(raw: str) -> Any:
    """从模型返回中尽力解析出 json 对象（兼容 ```json 包裹与前后多余文本）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
            return data
        except json.JSONDecodeError:
            return None


def to_bool(value: Any, default: bool = False) -> bool:
    """把模型可能返回的 true/1/"true"/"是" 等统一转成 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "是"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def to_tristate(value: Any) -> bool | None:
    """三态解析：明确 true → True，明确 false → False，缺失 / null 返回 None。"""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "unknown", "n/a"}:
        return None
    return to_bool(value)


def to_optional_bool(value: Any) -> bool | None:
    """只把明确的 true / false 转成 bool；「不确定」保持 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "是", "真的", "有效", "真实"}:
            return True
        if text in {"false", "0", "no", "n", "不是", "假的", "无效", "否", "不真实"}:
            return False
    return None


def normalize_verify_status(value: Any) -> str:
    """把模型返回的真假状态统一成 valid / uncertain / invalid / empty。"""
    if isinstance(value, bool):
        return VERIFY_VALID if value else VERIFY_INVALID
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"valid", "true", "yes", "real", "是", "真的", "真实", "有效"}:
            return VERIFY_VALID
        if text in {"invalid", "false", "no", "fake", "不是", "假的", "虚构", "不真实", "无效", "否"}:
            return VERIFY_INVALID
        if text in {"empty", "missing", "none", "未填写", "空"}:
            return VERIFY_EMPTY
    return VERIFY_UNCERTAIN


def clean_items(values: Any, limit: int = VERIFY_ITEM_LIMIT) -> list[str]:
    """把模型返回的候选数组去空格、去重、最多保留 limit 个。"""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    items: list[str] = []
    for value in values:
        text = str(value).strip() if isinstance(value, (str, int, float)) else ""
        if text and text not in items:
            items.append(text)
    return items[:limit]


def parse_question(raw: str) -> str:
    """解析出题 / 重写的返回，取出题目文本。"""
    data = load_json_object(raw)
    if isinstance(data, dict):
        for key in ("question", "q", "content", "text"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    text = (raw or "").strip()
    # 完全没法解析成 json 时，把原文当作题目，避免流程中断
    return "" if (not text or text.startswith("{")) else text


def parse_vote(raw: str) -> dict[str, Any] | None:
    """解析评审投票 {"pass": bool, "reason": str}；无法解析返回 None（视为弃权）。"""
    data = load_json_object(raw)
    if not isinstance(data, dict):
        return None
    return {
        "pass": to_tristate(data.get("pass", data.get("passed"))),
        "reason": str(data.get("reason") or data.get("comment") or "").strip(),
    }


def clamp_dimension(value: Any, max_score: int) -> int | None:
    """把单个维度分数裁剪到 0..max；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none"}:
        return None
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(max_score, number))


def fallback_dimensions(total: int) -> dict[str, int]:
    """兼容旧版只返回总分：按 3/3/2/2 的权重把总分拆成四个维度。"""
    total = max(0, min(SCORE_TOTAL_MAX, int(total)))
    dims: dict[str, int] = {}
    remaining = total
    for key in DIMENSION_ORDER:
        max_score = int(SCORE_DIMENSIONS[key]["max"])
        share = round(total * max_score / SCORE_TOTAL_MAX)
        value = min(max_score, max(0, share), remaining)
        dims[key] = value
        remaining -= value
    for key in DIMENSION_ORDER:
        if remaining <= 0:
            break
        max_score = int(SCORE_DIMENSIONS[key]["max"])
        add = min(max_score - dims[key], remaining)
        dims[key] += add
        remaining -= add
    return dims


def parse_score(raw: str) -> dict[str, Any] | None:
    """解析评委会评分（四个维度 + comment + continue）；无法解析返回 None。

    维度满分：技术深度 3、项目经验 3、沟通表达 2、逻辑思维 2，四项相加 = 本题总分。
    兼容旧版只返回 score 的格式：按 3/3/2/2 权重拆分。
    """
    data = load_json_object(raw)
    if not isinstance(data, dict):
        return None

    dimensions: dict[str, int] = {}
    has_dimension = False
    for key, meta in SCORE_DIMENSIONS.items():
        value = clamp_dimension(data.get(key), int(meta["max"]))
        if value is not None:
            has_dimension = True
            dimensions[key] = value

    score: Any = data.get("score")
    if isinstance(score, str) and score.strip().lower() in {"", "null", "none"}:
        score = None
    try:
        score = int(round(float(score))) if score is not None else None
    except (TypeError, ValueError):
        score = None
    if score is not None:
        score = max(1, min(SCORE_TOTAL_MAX, score))

    if has_dimension:
        for key in DIMENSION_ORDER:
            dimensions.setdefault(key, 0)
        total = sum(dimensions[key] for key in DIMENSION_ORDER)
    elif score is not None:
        total = score
        dimensions = fallback_dimensions(score)
    else:
        total = None
        dimensions = {}

    return {
        "score": total,
        "dimensions": dimensions,
        "comment": str(data.get("comment") or "").strip(),
        "continue": to_tristate(data.get("continue")),
    }


def strip_markdown_code_fences(raw: str) -> str:
    """去掉模型返回里的 ```json / ``` 代码块标记。"""
    return re.sub(r"```\s*(?:json)?\s*", "", raw or "", flags=re.IGNORECASE).strip()


def _extract_dimensions_from_text(text: str) -> dict[str, int]:
    """从非标准返回文本里按维度名提取分数（Kimi 兜底用）。"""
    aliases: dict[str, tuple[str, ...]] = {
        "technical_depth": ("技术深度", "technical_depth", "技术"),
        "project_experience": ("项目经验", "project_experience", "项目"),
        "communication": ("沟通表达", "communication", "沟通"),
        "logical_thinking": ("逻辑思维", "logical_thinking", "逻辑"),
    }
    dimensions: dict[str, int] = {}
    for key, meta in SCORE_DIMENSIONS.items():
        for alias in aliases[key]:
            pattern = (
                rf"{re.escape(alias)}\"?\s*[（(]?\s*(?:[:：=]\s*)?"
                r"(\d+(?:\.\d+)?)\s*(?:/\s*\d+)?"
            )
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            value = clamp_dimension(match.group(1), int(meta["max"]))
            if value is not None:
                dimensions[key] = value
                break
    return dimensions


def _extract_total_score_from_text(text: str) -> int | None:
    """从文本里提取总分，支持「总分：7/10」「评分 8」「7 分」等写法。"""
    patterns = (
        r"(?:总分|总得分|最终得分|综合得分|评分|得分|score)\s*[:：=]?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:/\s*10)?",
        r"(\d+(?:\.\d+)?)\s*/\s*10",
        r"(?:^|\s)(\d+(?:\.\d+)?)\s*(?:分|points?)",
        r"(\d+(?:\.\d+)?)\s*(?:分|points?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            value = int(round(float(match.group(1))))
        except (TypeError, ValueError):
            continue
        if 0 <= value <= SCORE_TOTAL_MAX:
            return max(1, value)
    return None


def _extract_comment_from_text(text: str) -> str:
    """从非标准返回文本里提取评语字段；避免把整段 JSON 当评语。"""
    match = re.search(
        r'"?comment"?\s*[:：]\s*"([^"]*)"', text, re.IGNORECASE | re.DOTALL
    )
    if match:
        return match.group(1).strip()
    match = re.search(r"(?:评语|点评)\s*[:：]\s*(.+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"')
    return ""


def _extract_continue_from_text(text: str) -> bool | None:
    match = re.search(
        r'"?continue"?\s*[:：=]\s*(true|false|1|0)', text, re.IGNORECASE
    )
    if not match:
        return None
    return to_bool(match.group(1))


def parse_kimi_score_fallback(raw: str) -> dict[str, Any]:
    """Kimi 专用兜底：正则提分；完全提不出来时默认 5 分，不弃权。"""
    text = strip_markdown_code_fences(raw)
    dimensions = _extract_dimensions_from_text(text)
    total = _extract_total_score_from_text(text)
    defaulted = False

    if len(dimensions) == len(DIMENSION_ORDER):
        for key in DIMENSION_ORDER:
            dimensions.setdefault(key, 0)
        total = sum(dimensions[key] for key in DIMENSION_ORDER)
    elif total is not None:
        dimensions = fallback_dimensions(total)
    elif dimensions:
        for key in DIMENSION_ORDER:
            dimensions.setdefault(key, 0)
        total = sum(dimensions[key] for key in DIMENSION_ORDER)
    else:
        total = 5
        dimensions = fallback_dimensions(total)
        defaulted = True

    comment = _extract_comment_from_text(text)
    if defaulted:
        comment = "评分格式解析异常"
    elif not comment:
        lowered = text.lower()
        looks_like_json = any(
            token in lowered
            for token in (
                "{",
                "}",
                "technical_depth",
                "project_experience",
                "communication",
                "logical_thinking",
                "score",
                "comment",
                "continue",
            )
        )
        comment = (
            "Kimi 返回格式异常，已从文本中提取分数。"
            if looks_like_json
            else (text or "Kimi 返回格式异常，已从文本中提取分数。")
        )

    return {
        "score": total,
        "dimensions": dimensions,
        "comment": comment,
        "continue": _extract_continue_from_text(text),
    }


def parse_score_for_judge(raw: str, judge: str) -> dict[str, Any] | None:
    """按评委解析评分；Kimi 额外做去代码块 / 正则 / 默认 5 分兜底。"""
    if judge != "kimi":
        return parse_score(raw)
    cleaned = strip_markdown_code_fences(raw)
    parsed = parse_score(cleaned)
    if parsed is not None and parsed.get("score") is not None:
        return parsed
    return parse_kimi_score_fallback(cleaned)


def parse_resume_match(raw: str) -> dict[str, Any] | None:
    """解析简历匹配度评分；无法解析时返回 None（不阻塞报告）。"""
    data = load_json_object(raw)
    if not isinstance(data, dict):
        return None
    score: Any = data.get("resume_match_score")
    try:
        score = int(round(float(score)))
    except (TypeError, ValueError):
        return None
    return {
        "score": max(0, min(100, score)),
        "comment": str(data.get("resume_match_comment") or "").strip(),
    }


def parse_suggestions(raw: str, field: str, limit: int = SUGGEST_ITEM_LIMIT) -> list[str]:
    """解析输入纠错返回的候选数组（去空格、去重、最多 limit 个）；无法解析时返回 []。"""
    data = load_json_object(raw)
    if not isinstance(data, dict):
        return []
    return clean_items(data.get(field), limit)


def _parse_verify_section(
    value: Any, legacy_values: Any = None, *, empty: bool = False
) -> dict[str, Any]:
    """解析 job / company 单个校验结果，兼容模型只返回字符串状态或旧版候选数组。"""
    if empty:
        return {"status": VERIFY_EMPTY, "reason": "未填写公司", "suggestions": []}
    if isinstance(value, str):
        status = normalize_verify_status(value)
        value = {}
    else:
        status = VERIFY_UNCERTAIN
    if not isinstance(value, dict):
        value = {}
    if value:
        status = normalize_verify_status(
            value.get("status", value.get("valid", value.get("is_real")))
        )
    suggestions = clean_items(
        value.get(
            "suggestions",
            value.get("candidates", value.get("items", legacy_values)),
        )
    )
    return {
        "status": status,
        "reason": str(value.get("reason") or value.get("comment") or "").strip(),
        "suggestions": suggestions,
    }


def parse_input_check(raw: str, company: str = "") -> dict[str, dict[str, Any]] | None:
    """解析岗位 / 公司真实性校验结果；返回 None 表示模型输出不可用。

    兼容旧格式（只有 suggested_jobs / suggested_companies、没有 status）：
    此时按 uncertain 处理，用户必须从真实候选里选一个。
    """
    data = load_json_object(raw)
    if not isinstance(data, dict):
        return None
    known = {"job", "company", "suggested_jobs", "suggested_companies"}
    if not (known & set(data)):
        return None
    return {
        "job": _parse_verify_section(data.get("job"), data.get("suggested_jobs")),
        "company": _parse_verify_section(
            data.get("company"),
            data.get("suggested_companies"),
            empty=not company.strip(),
        ),
    }


def parse_resume_check(raw: str) -> dict[str, Any] | None:
    """解析简历有效性校验结果；返回 None 表示模型输出无法解析。"""
    data = load_json_object(raw)
    if not isinstance(data, dict):
        return None
    if not any(key in data for key in ("is_resume", "is_valid", "valid", "status")):
        return None
    flag = data.get("is_resume", data.get("is_valid", data.get("valid")))
    is_resume = to_optional_bool(flag)
    if is_resume is None:
        status = normalize_verify_status(data.get("status"))
        if status == VERIFY_VALID:
            is_resume = True
        elif status == VERIFY_INVALID:
            is_resume = False
    return {
        "is_resume": is_resume,
        "reason": str(data.get("reason") or data.get("comment") or "").strip(),
    }


def suggestion_signature(job: str, company: str) -> str:
    """按当前输入生成短签名：输入一变，单选控件的 key 就换，不会残留上一次的选择。"""
    return hashlib.md5(f"{job}\x00{company}".encode("utf-8")).hexdigest()[:8]


def clean_reverse_answer(raw: str) -> str:
    """兜底：模型偶尔仍以 json 回复反问，这里尽量取出其中的回答正文。"""
    text = (raw or "").strip()
    if text.startswith("{") and text.endswith("}"):
        data = load_json_object(text)
        if isinstance(data, dict):
            for key in ("answer", "reply", "content", "response", "text"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return text


def is_reverse_finished(text: str) -> bool:
    """判断候选人是否在反问环节表示「没有其他问题了」。"""
    normalized = re.sub(r"[\s，。！？!?、,.~～…]+", "", text or "").lower()
    if not normalized:
        return False
    return normalized in REVERSE_END_EXACT or any(key in normalized for key in REVERSE_END_CONTAINS)


def trimmed_average(scores: list[int]) -> float | None:
    """去掉一个最高分、一个最低分，剩下的取平均（本题最终分）。

    6 位评委都成功时应剩 4 个分数；不足 3 个有效分数时不裁剪。
    """
    if not scores:
        return None
    usable = sorted(scores)
    if len(usable) >= 3:
        usable = usable[1:-1]
    return round(sum(usable) / len(usable), 1)


def average_dimensions(dimension_sets: Iterable[dict[str, int]]) -> dict[str, float]:
    """把多组维度分按维度求平均（用于本题 / 全场维度分析）。"""
    buckets: dict[str, list[float]] = {key: [] for key in DIMENSION_ORDER}
    for dimensions in dimension_sets:
        if not isinstance(dimensions, dict):
            continue
        for key in DIMENSION_ORDER:
            value = dimensions.get(key)
            if isinstance(value, (int, float)):
                buckets[key].append(float(value))
    return {
        key: round(sum(values) / len(values), 1) if values else 0.0
        for key, values in buckets.items()
    }


def dimension_summary(dimensions: dict[str, int] | dict[str, float] | None) -> str:
    """把维度分拼成一行紧凑文案，例如「技术深度 2/3 · 项目经验 3/3 …」。"""
    dimensions = dimensions or {}
    parts = []
    for key, meta in SCORE_DIMENSIONS.items():
        value = dimensions.get(key)
        text = f"{float(value):g}" if isinstance(value, (int, float)) else "—"
        parts.append(f"{meta['label']} {text}/{meta['max']}")
    return " · ".join(parts)


def judge_score_value_markdown(result: dict[str, Any]) -> str:
    """右侧评委卡片上只显示分数（总分 / 10）。"""
    score = result.get("score")
    score_text = f"{score}" if isinstance(score, (int, float)) else "—"
    return f"**{score_text} / {SCORE_TOTAL_MAX}**"


def judge_dimension_table_markdown(result: dict[str, Any]) -> str:
    """四个维度的紧凑表格，用于展开后的评分详情。"""
    dimensions = result.get("dimensions") or {}
    header = "| " + " | ".join(meta["label"] for meta in SCORE_DIMENSIONS.values()) + " |"
    divider = "| " + " | ".join(":---:" for _ in SCORE_DIMENSIONS) + " |"
    cells = []
    for key, meta in SCORE_DIMENSIONS.items():
        value = dimensions.get(key)
        value_text = f"{value}" if isinstance(value, (int, float)) else "—"
        cells.append(f"{value_text}/{meta['max']}")
    return "\n".join([header, divider, "| " + " | ".join(cells) + " |"])


def judge_comment_text(result: dict[str, Any]) -> str:
    """评委评语；失败时退回错误信息，但仍不展示 JSON 原文。"""
    return str(result.get("comment") or result.get("error") or "").strip() or "（没有评语）"


def judge_detail_markdown(result: dict[str, Any]) -> str:
    """展开卡片后的内容：四维度分 + 单独一段评语。"""
    return (
        f"**维度**　{dimension_summary(result.get('dimensions'))}\n\n"
        f"**评语**\n\n{judge_comment_text(result)}"
    )


def judge_score_card_markdown(result: dict[str, Any]) -> str:
    """单个评委评分卡片的完整正文（兼容旧布局与测试）。"""
    score = result.get("score")
    score_text = f"{score}" if isinstance(score, (int, float)) else "—"
    return (
        f"**评分：{score_text} / {SCORE_TOTAL_MAX}**\n\n"
        f"{judge_detail_markdown(result)}"
    )


def render_judge_score_card(card: Any, text: str, err: str, judge: str = "") -> None:
    """单个评委完成后立刻刷新自己的卡片；解析失败也绝不显示 JSON 原文。"""
    if err:
        card.markdown(f"❌ {err}")
        return
    parsed = parse_score_for_judge(text, judge)
    if parsed is None:
        card.markdown("⚠️ 评分格式无法解析。")
        return
    if parsed.get("score") is None:
        card.markdown("⚠️ 这位评委没有给出有效评分。")
        return
    card.markdown(judge_score_card_markdown(parsed))


def judge_label(judge: str) -> str:
    """评委展示名。"""
    return JUDGE_META.get(judge, {}).get("label", judge)


# --------------------------------------------------------------------------- #
# Prompt 拼装
# --------------------------------------------------------------------------- #
def interview_context() -> dict[str, str]:
    """取本场面试的配置（岗位 / 模式 / 公司规模 / 企业名 / 简历文本）。"""
    state = st.session_state
    return {
        "job": state.job,
        "mode": state.mode,
        "scale": state.scale,
        "company": state.company,
        "resume_text": state.resume_text,
    }


def build_context_block(context: dict[str, str]) -> str:
    """所有评委共用的背景块：岗位 + 模式 + 公司规模 + 目标企业 + 简历参考。"""
    company = (context.get("company") or "").strip()
    company_line = (
        f"- 目标企业：{company}（提问与回答都用「我们{company}」的口吻，"
        f"例如「我们是{company}，你对推荐算法了解多少」）"
        if company
        else "- 目标企业：未提供（用该行业通用口吻，不要虚构或猜测公司名）"
    )
    resume_text = (context.get("resume_text") or "").strip()
    if resume_text:
        resume_block = (
            "以下是候选人的简历（<简历> 内为 PyPDF2 解析出的原文），请作为出题 / 评审 / 打分的依据之一，"
            "问题要能核对简历里的真实经历（不要逐条复述简历）：\n"
            f"<简历>\n{resume_text}\n</简历>"
        )
    else:
        resume_block = "候选人未上传简历，完全依据现场回答判断。"

    track = detect_job_track(context["job"])
    lines = [
        "# 面试背景",
        f"- 应聘岗位：{context['job']}",
        f"- 岗位方向：{track['label']}",
        f"- 本岗位出题范围（必须优先覆盖）：{track['focus']}",
        f"- 面试模式：{context['mode']} —— {INTERVIEW_MODES[context['mode']]['desc']}",
        f"- 公司规模：{context['scale']} —— {COMPANY_SCALES[context['scale']]['desc']}",
        company_line,
        "",
        "# 本场定位",
        INTERVIEW_MODES[context["mode"]]["style"],
        COMPANY_SCALES[context["scale"]]["style"],
        "",
        "# 岗位出题范围",
        f"{track['focus']}",
        "出题必须覆盖上述范围内的不同子主题，不要问与该岗位方向无关的问题；"
        "如果岗位方向与简历经历不完全一致，以岗位要求为主、简历经历为辅。",
        "",
        "# 简历参考",
        resume_block,
    ]
    return "\n".join(lines)


def build_asked_summary(asked: list[str]) -> str:
    """把已经问过的问题编号列出，避免评委重复出题。"""
    if not asked:
        return "（第 1 题，还没有问过任何问题；可以先从自我介绍或最相关的项目经历切入）"
    return "\n".join(f"{i}. {q}" for i, q in enumerate(asked, start=1))


def build_writer_messages(
    judge: str, index: int, asked: list[str], context: dict[str, str]
) -> list[dict[str, str]]:
    """主考官出题的 messages。"""
    system = WRITER_SYSTEM_TEMPLATE.format(
        label=judge_label(judge),
        index=index,
        persona=JUDGE_META[judge]["persona"],
        context=build_context_block(context),
    )
    user = WRITER_USER_TEMPLATE.format(
        index=index, min_q=MIN_QUESTIONS, max_q=MAX_QUESTIONS, asked=build_asked_summary(asked)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_review_messages(
    judge: str,
    index: int,
    writer: str,
    question: str,
    asked: list[str],
    round_no: int,
    kind: str,
    context: dict[str, str],
) -> list[dict[str, str]]:
    """评审团投票的 messages（宽松标准：方向对、不离谱、岗位相关就通过）。"""
    system = REVIEW_SYSTEM_TEMPLATE.format(context=build_context_block(context))
    user = REVIEW_USER_TEMPLATE.format(
        round=round_no, kind=kind, asked=build_asked_summary(asked), question=question
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_rewrite_messages(
    judge: str, index: int, question: str, reasons: str, asked: list[str], context: dict[str, str]
) -> list[dict[str, str]]:
    """投反对票的评委重写题目的 messages。"""
    system = REWRITE_SYSTEM_TEMPLATE.format(
        label=judge_label(judge),
        persona=JUDGE_META[judge]["persona"],
        context=build_context_block(context),
    )
    user = REWRITE_USER_TEMPLATE.format(
        index=index,
        question=question,
        reasons=reasons or "（评审未给出具体意见，请按岗位相关性自行优化）",
        asked=build_asked_summary(asked),
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_score_messages(
    judge: str, index: int, writer: str, question: str, answer: str, context: dict[str, str]
) -> list[dict[str, str]]:
    """评委打分的 messages。"""
    system = SCORE_SYSTEM_TEMPLATE.format(
        label=judge_label(judge),
        persona=JUDGE_META[judge]["persona"],
        context=build_context_block(context),
    )
    user = SCORE_USER_TEMPLATE.format(
        index=index, writer_label=judge_label(writer), question=question, answer=answer
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_reverse_messages(judge: str, question: str, context: dict[str, str]) -> list[dict[str, str]]:
    """反问环节的 messages（由本场面试官作答）。"""
    target = f"我们是{context['company']}" if context.get("company") else "我们公司"
    instruction = REVERSE_ANSWER_PROMPT.format(target=target, scale=context["scale"])
    system = f"你是「{judge_label(judge)}」，本场面试的面试官。{JUDGE_META[judge]['persona']}"
    user = f"{instruction}\n{REVERSE_PROMPT_MARK}{question}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_transcript(state: Any) -> str:
    """把逐题问答与各评委的打分整理成给模型看的文本。"""
    lines: list[str] = []
    for item in state.qa_history:
        lines.append(f"### 第 {item['index']} 题（主考官：{judge_label(item.get('writer', ''))}）")
        lines.append(f"问题：{item['question']}")
        lines.append(f"候选人回答：{item['answer']}")
        lines.append(f"本题最终分（去极值平均）：{item.get('score')}")
        results = item.get("judge_results", [])
        scores = "、".join(
            f"{judge_label(result['judge'])} {result['score'] if result['score'] is not None else '未评分'}"
            for result in results
        )
        lines.append(f"{len(results)} 位评委独立评分：{scores}")
        lines.append("")
    if state.reverse_history:
        lines.append("### 反问环节")
        for order, item in enumerate(state.reverse_history, start=1):
            lines.append(f"Q{order}：{item['question']} → {item['answer']}")
        lines.append("")
    return "\n".join(lines).strip() or "（本场没有问答记录）"


def build_resume_match_messages(
    judge: str, context: dict[str, str], transcript: str
) -> list[dict[str, str]]:
    """简历匹配度评分的 messages。"""
    judge_count = len(score_judges())
    system = f"你是「{judge_label(judge)}」评委。{JUDGE_META[judge]['persona']}"
    user = (
        f"{build_context_block(context)}\n\n## 本场问答与 {judge_count} 位评委打分\n{transcript}\n\n"
        f"{RESUME_MATCH_PROMPT.format(job=context['job'], judge_count=judge_count)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_verify_messages(job: str, company: str) -> list[dict[str, str]]:
    """岗位 / 公司真实性校验 + 候选推荐的 messages（固定调一次 DeepSeek）。"""
    user = VERIFY_USER_TEMPLATE.format(
        job=job,
        company=company or VERIFY_EMPTY_COMPANY,
        empty=VERIFY_EMPTY_COMPANY,
    )
    return [
        {"role": "system", "content": VERIFY_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def build_resume_check_messages(resume_text: str) -> list[dict[str, str]]:
    """简历有效性校验的 messages（固定调一次 DeepSeek）。"""
    return [
        {"role": "system", "content": RESUME_CHECK_SYSTEM_PROMPT},
        {"role": "user", "content": RESUME_CHECK_USER_TEMPLATE.format(text=resume_text)},
    ]


def build_suggest_messages(job: str, company: str) -> list[dict[str, str]]:
    """兼容旧名字：现在同时做真实性校验 + 候选推荐。"""
    return build_verify_messages(job, company)


# --------------------------------------------------------------------------- #
# 本场架构：分层调用 / 快速模式
# --------------------------------------------------------------------------- #
def quick_mode() -> bool:
    """本场是否勾选了「快速模式」（只用一个小模型完成出题 + 打分）。"""
    try:
        return bool(st.session_state.get("quick_mode"))
    except Exception:  # noqa: BLE001 —— 脱离 Streamlit 运行时（单测）按「非快速模式」处理
        return False


def score_judges() -> list[str]:
    """打分阶段的评委：打分保质量，正常模式 6 家全上；快速模式只用 1 个快模型。"""
    return [QUICK_MODE_JUDGE] if quick_mode() else list(JUDGE_ORDER)


def writer_pool() -> list[str]:
    """出题阶段的主考官候选：正常模式只用 3 个快模型，快速模式只 1 个。"""
    return [QUICK_MODE_JUDGE] if quick_mode() else list(FAST_JUDGES)


def review_panel(writer: str) -> list[str]:
    """评审团：5 位评委（3 个快模型 + DeepSeek + 通义），≥REVIEW_PASS_VOTES 票通过才发题。

    快速模式没有独立评审团，直接返回空列表。
    """
    if quick_mode():
        return []
    panel = list(REVIEW_JUDGES)
    if writer not in panel:
        panel = [writer, *panel[: REVIEW_TEAM_SIZE - 1]]
    return panel[:REVIEW_TEAM_SIZE]


def review_size(item: dict[str, Any]) -> int:
    """这道题的评审团人数（快速模式没有独立评审团，返回 0）。"""
    rounds = item.get("rounds") or []
    return len(rounds[0]["votes"]) if rounds else 0


def judge_preference() -> list[str]:
    """只需要「一位评委」时的候选顺序（反问回答 / 简历匹配度 / 综合点评）。"""
    return [QUICK_MODE_JUDGE] if quick_mode() else list(JUDGE_ORDER)


def architecture_label() -> str:
    """本场调用架构文案：快速模式（1 个小模型）/ 分层（3 个快模型出题 + 5 位评审 + 6 家打分）。"""
    if quick_mode():
        return f"快速模式：{JUDGE_META[QUICK_MODE_JUDGE]['label']} 单模型"
    return (
        f"分层调用：{len(FAST_JUDGES)} 个快模型出题 + {REVIEW_TEAM_SIZE} 位评审 + "
        f"{len(JUDGE_ORDER)} 位评委打分"
    )


def build_summary_messages(judge: str, facts: str, records: str) -> list[dict[str, str]]:
    """AI 综合点评（招聘委员会主审）的 messages。"""
    system = (
        f"{SUMMARY_SYSTEM_PROMPT.format(judge_count=len(score_judges()))}\n"
        f"本次由「{judge_label(judge)}」代表委员会执笔。"
    )
    user = SUMMARY_USER_TEMPLATE.format(facts=facts, records=records)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# 出题流程：快模型出题 → 5 位评审并发投票（≥3 票通过） → 反对的评委并发重写（最多 1 次）
# --------------------------------------------------------------------------- #
def generate_question(
    index: int, asked: list[str], context: dict[str, str]
) -> tuple[str, str, str]:
    """只在 3 个快模型里随机抽 1 位当主考官出题；失败时依次换人兜底。返回 (主考官, 题目, 错误)。"""
    pool = writer_pool()
    writer = random.choice(pool)
    order = [writer] + [name for name in pool if name != writer]
    last_error = ""
    for name in order:
        text, err = call_one(
            name,
            build_writer_messages(name, index, asked, context),
            json_mode=True,
            max_tokens=QUESTION_MAX_TOKENS,
        )
        if err:
            last_error = err
            continue
        question = parse_question(text)
        if question:
            return name, question, ""
        last_error = f"{judge_label(name)} 没有返回可用的题目。"
    return writer, "", last_error or "出题模型都没有给出可用的题目。"


def collect_votes(
    panel: list[str],
    index: int,
    writer: str,
    question: str,
    asked: list[str],
    round_no: int,
    kind: str,
    context: dict[str, str],
) -> list[dict[str, Any]]:
    """评审团并发投票：一次把 5 位评审的请求同时发出去（题目短，限制 200 token）。"""
    results = call_many(
        panel,
        lambda name: build_review_messages(
            name, index, writer, question, asked, round_no, kind, context
        ),
        json_mode=True,
        max_tokens=QUESTION_MAX_TOKENS,
    )
    votes: list[dict[str, Any]] = []
    for name in panel:
        text, err = results.get(name, ("", "未知错误"))
        if err:
            votes.append({"judge": name, "pass": False, "reason": "", "error": err})
            continue
        parsed = parse_vote(text)
        if parsed is None:
            votes.append({"judge": name, "pass": None, "reason": text[:120], "error": ""})
            continue
        votes.append(
            {"judge": name, "pass": parsed["pass"], "reason": parsed["reason"], "error": ""}
        )
    return votes


def rewrite_question(
    voters: list[str],
    index: int,
    question: str,
    votes: list[dict[str, Any]],
    asked: list[str],
    context: dict[str, str],
) -> str:
    """投「不通过」的评委并发重写；取第一位给出可用题目的评委结果。"""
    if not voters:
        return ""
    reasons = {vote["judge"]: vote["reason"] for vote in votes}
    results = call_many(
        voters,
        lambda name: build_rewrite_messages(
            name, index, question, reasons.get(name, ""), asked, context
        ),
        json_mode=True,
        max_tokens=QUESTION_MAX_TOKENS,
    )
    for name in voters:
        text, err = results.get(name, ("", ""))
        if err:
            continue
        candidate = parse_question(text)
        if candidate:
            return candidate
    return ""


def review_question(
    index: int, writer: str, original: str, asked: list[str], context: dict[str, str]
) -> dict[str, Any]:
    """评审团投票（5 位评审，≥3 票通过），不通过则由反对的评委重写并再投票（最多 1 次）。

    1 次重写仍不通过时，直接使用主考官的原题。
    快速模式没有独立评审团：主考官（唯一的小模型）出的题直接发给用户。
    """
    panel = review_panel(writer)
    if not panel:
        return {
            "question": original,
            "passed": True,
            "used_original": True,
            "rewrites": 0,
            "rounds": [],
            "errors": [],
        }
    current = original
    kind = "主考官原题"
    rounds: list[dict[str, Any]] = []
    errors: list[str] = []
    rewrites = 0

    for attempt in range(MAX_REWRITES + 1):
        round_no = attempt + 1
        votes = collect_votes(panel, index, writer, current, asked, round_no, kind, context)
        errors.extend(vote["error"] for vote in votes if vote["error"])
        passes = sum(1 for vote in votes if vote["pass"] is True)
        failed = [
            vote["judge"] for vote in votes if vote["pass"] is False and not vote["error"]
        ]
        record: dict[str, Any] = {
            "round": round_no,
            "question": current,
            "kind": kind,
            "votes": votes,
            "passes": passes,
            "total": len(panel),
            "failed": failed,
            "rewriters": [],
        }
        rounds.append(record)

        if passes >= REVIEW_PASS_VOTES:
            return {
                "question": current,
                "passed": True,
                "used_original": current == original,
                "rewrites": rewrites,
                "rounds": rounds,
                "errors": errors,
            }
        if attempt == MAX_REWRITES:
            break
        if not failed:
            break  # 没有明确的反对者（多为调用失败），重写也没有意义
        new_question = rewrite_question(failed, index, current, votes, asked, context)
        if not new_question:
            break  # 重写全部失败：保留当前题目
        record["rewriters"] = failed
        current = new_question
        rewrites += 1
        kind = f"第 {rewrites} 次重写"

    return {
        "question": original,
        "passed": False,
        "used_original": True,
        "rewrites": rewrites,
        "rounds": rounds,
        "errors": errors,
    }


def ask_next_question() -> bool:
    """协作出题并写入 session_state。成功返回 True。"""
    state = st.session_state
    index = len(state.qa_history) + 1
    asked = [item["question"] for item in state.qa_history]
    context = interview_context()

    writer, original, err = generate_question(index, asked, context)
    if not original:
        state.error = err or "6 位评委都没有给出可用的题目，请稍后重试。"
        return False

    review = review_question(index, writer, original, asked, context)
    state.current_question = review["question"]
    state.current_question_meta = {
        "index": index,
        "writer": writer,
        "original": original,
        "rewrites": review["rewrites"],
        "passed": review["passed"],
        "used_original": review["used_original"],
        "rounds": review["rounds"],
    }
    state.awaiting_answer = True
    state.error = ""
    if review["errors"]:
        state.error = "部分评委调用失败（已按剩余评委继续）：" + "；".join(review["errors"][:2])
    return True


# --------------------------------------------------------------------------- #
# 评分流程：6 位评委并发打分 → 去掉最高/最低取平均 → 统计 continue 票数
# --------------------------------------------------------------------------- #
def build_score_outcome(
    results: dict[str, tuple[str, str]], judges: list[str]
) -> dict[str, Any]:
    """把 {评委: (原文, 错误)} 整理成逐评委维度分、本题最终分与 continue=false 票数。"""
    judge_results: list[dict[str, Any]] = []
    errors: list[str] = []
    for name in judges:
        text, err = results.get(name, ("", "未知错误"))
        if err:
            errors.append(err)
            entry = {
                "judge": name,
                "score": None,
                "dimensions": {},
                "comment": "",
                "continue": None,
                "error": err,
            }
            judge_results.append(entry)
            continue
        parsed = parse_score_for_judge(text, name)
        if parsed is None:
            errors.append(f"{judge_label(name)} 的评分无法解析。")
            entry = {
                "judge": name,
                "score": None,
                "dimensions": {},
                "comment": "",
                "continue": None,
                "error": "评分格式无法解析",
            }
            judge_results.append(entry)
            continue
        entry = {
            "judge": name,
            "score": parsed["score"],
            "dimensions": parsed["dimensions"],
            "comment": parsed["comment"],
            "continue": parsed["continue"],
            "error": "",
        }
        judge_results.append(entry)

    scores = [item["score"] for item in judge_results if item["score"] is not None]
    false_votes = sum(1 for item in judge_results if item["continue"] is False)
    reason = "；".join(
        f"{judge_label(item['judge'])}：{item['comment'] or '判定不合格'}"
        for item in judge_results
        if item["continue"] is False
    )
    return {
        "judge_results": judge_results,
        "score": trimmed_average(scores),
        "dimensions": average_dimensions(item["dimensions"] for item in judge_results),
        "false_votes": false_votes,
        "reason": reason,
        "errors": errors,
    }


def score_answer(
    index: int, question: str, answer: str, writer: str, context: dict[str, str]
) -> dict[str, Any]:
    """打分阶段的评委并发打分（快速模式 1 位 / 正常模式 6 位），返回本题结果。

    一次把所有人的请求同时发出去；max_tokens 限制在 300（四个维度分 + 短评语）。
    """
    judges = score_judges()
    results = call_many(
        judges,
        lambda name: build_score_messages(name, index, writer, question, answer, context),
        json_mode=True,
        max_tokens=SCORE_MAX_TOKENS,
    )
    return build_score_outcome(results, judges)


def create_live_judge_slots(judges: list[str], labels: dict[str, str]) -> dict[str, Any]:
    """没有右侧面板时（单测 / 兜底）创建一组最简评分占位卡片。"""
    slots: dict[str, Any] = {}
    for name in judges:
        with st.container(key=f"judge_card_{name}"):
            st.markdown(f"**{labels[name]}**")
            score_ph = st.empty()
            with st.expander("查看评语", expanded=False):
                comment_ph = st.empty()
        slots[name] = {"score": score_ph, "comment": comment_ph}
    slots["__summary__"] = st.empty()
    return slots


def update_judge_slot(slot: dict[str, Any], judge: str, text: str, err: str) -> None:
    """把单个评委结果刷到「分数 + 折叠评语」两个占位符里。"""
    score_ph = slot["score"]
    comment_ph = slot["comment"]
    if err:
        score_ph.markdown("**—**")
        comment_ph.markdown(f"❌ {err}")
        return
    parsed = parse_score_for_judge(text, judge)
    if parsed is None:
        score_ph.markdown("**—**")
        comment_ph.markdown("⚠️ 评分格式无法解析。")
        return
    if parsed.get("score") is None:
        score_ph.markdown("**—**")
        comment_ph.markdown("⚠️ 这位评委没有给出有效评分。")
        return
    score_ph.markdown(judge_score_value_markdown(parsed))
    comment_ph.markdown(judge_detail_markdown(parsed))


def score_answer_stream(
    index: int,
    question: str,
    answer: str,
    writer: str,
    context: dict[str, str],
    slots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """打分 + 实时更新右侧评委卡片（评委之间并发，评完一位刷新一位）。"""
    judges = score_judges()
    labels = {name: judge_label(name) for name in judges}
    if slots is None:
        try:
            slots = create_live_judge_slots(judges, labels)
        except Exception:  # noqa: BLE001 —— 不在渲染上下文（单测）时退回一次性打分
            return score_answer(index, question, answer, writer, context)

    for name in judges:
        slot = slots.get(name)
        if slot:
            slot["score"].markdown("⏳ 评分中")
            slot["comment"].markdown("")
    summary_slot = slots.get("__summary__")
    if summary_slot:
        summary_slot.markdown("**⏳ 正在评分...**")

    clients = get_clients()  # 在主线程取（st.cache_resource 不适合在子线程调用）

    def worker(name: str) -> tuple[str, str]:
        return stream_judge(
            clients[name],
            name,
            build_score_messages(name, index, writer, question, answer, context),
            lambda _text: None,
            json_mode=True,
            temperature=1.0,
            max_tokens=SCORE_MAX_TOKENS,
        )

    hydrate_working_models()
    results: dict[str, tuple[str, str]] = {}
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS + 3
    pool = ThreadPoolExecutor(max_workers=len(judges), thread_name_prefix="judge")
    try:
        futures = {pool.submit(worker, name): name for name in judges}
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in done:
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:  # noqa: BLE001 —— 单评委异常不影响其他评委
                    results[name] = ("", f"{labels[name]} 调用异常：{exc}")
                slot = slots.get(name)
                if slot:
                    update_judge_slot(slot, name, *results[name])
            if pending and time.monotonic() > deadline:
                for future in pending:
                    name = futures[future]
                    results[name] = (
                        "",
                        f"{labels[name]} 超时（>{REQUEST_TIMEOUT_SECONDS}s），已跳过",
                    )
                    slot = slots.get(name)
                    if slot:
                        update_judge_slot(slot, name, *results[name])
                pending.clear()
    finally:
        pool.shutdown(wait=False)  # 到点了就返回，不等还在跑的线程
    sync_working_models_to_state()

    outcome = build_score_outcome(results, judges)
    if summary_slot:
        if outcome["score"] is not None:
            summary_slot.markdown(
                f"**本题平均分　{outcome['score']:.1f} / {SCORE_TOTAL_MAX}**"
            )
        else:
            summary_slot.caption("没有有效评分")
    return outcome


def finish_questions() -> None:
    """问答环节结束，进入反问环节。"""
    state = st.session_state
    state.awaiting_answer = False
    state.current_question = ""
    state.current_question_meta = {}
    state.phase = "reverse"


def start_interview(
    job: str,
    mode: str,
    scale: str,
    company: str = "",
    resume_text: str = "",
    resume_name: str = "",
    quick: bool = False,
) -> bool:
    """写入本场配置并出第 1 题（快模型主考官 + 评审团投票）。成功返回 True。"""
    st.session_state.update(
        {
            "stage": "interview",
            "phase": "questions",
            "job": job,
            "mode": mode,
            "scale": scale,
            "quick_mode": bool(quick),
            "company": company,
            "resume_name": resume_name,
            "resume_text": resume_text,
            "qa_history": [],
            "reverse_history": [],
            "current_question": "",
            "current_question_meta": {},
            "awaiting_answer": False,
            "eliminated": False,
            "eliminate_reason": "",
            "closed_by": "",
            "resume_match": None,
            "report": "",
            "report_error": "",
            "error": "",
            "confirm_end": False,
            "history_saved": False,
            "history_record_id": 0,
            "history_error": "",
            "show_history": False,
        }
    )
    if not ask_next_question():
        st.session_state.stage = "setup"  # 连第 1 题都出不来就退回设置页
        return False
    return True


def submit_answer(answer: str, judge_slots: dict[str, Any] | None = None) -> None:
    """记录本轮回答 → 6 位评委并发打分 → 决定继续 / 淘汰 / 收尾。"""
    state = st.session_state
    meta = state.current_question_meta or {}
    index = int(meta.get("index") or (len(state.qa_history) + 1))
    context = interview_context()

    record: dict[str, Any] = {
        "index": index,
        "question": state.current_question,
        "answer": answer,
        "writer": meta.get("writer", ""),
        "original": meta.get("original", state.current_question),
        "rewrites": meta.get("rewrites", 0),
        "passed": meta.get("passed", True),
        "used_original": meta.get("used_original", True),
        "rounds": meta.get("rounds", []),
        "judge_results": [],
        "score": None,
        "dimensions": {},
        "false_votes": 0,
        "reason": "",
        "closed_reason": "",
    }
    state.qa_history.append(record)

    results = score_answer_stream(
        index, record["question"], answer, record["writer"], context, slots=judge_slots
    )
    if results["score"] is None and results["false_votes"] == 0 and results["errors"]:
        # 一个有效评分都没拿到：回滚本轮，让候选人重新提交
        state.qa_history.pop()
        state.error = (
            f"{len(score_judges())} 位评委都没有成功评分，请检查网络或 API Key 后重新提交。"
            + "；".join(results["errors"][:2])
        )
        return

    record["judge_results"] = results["judge_results"]
    record["score"] = results["score"]
    record["dimensions"] = results["dimensions"]
    record["false_votes"] = results["false_votes"]
    record["reason"] = results["reason"]

    warnings = ""
    if results["errors"]:
        warnings = "部分评委评分失败（已按其余评委计算本题分数）：" + "；".join(results["errors"][:2])

    # 1) ≥2 位评委投 continue=false → 立即结束面试并出报告（不足最少题量时不淘汰）
    if index >= MIN_QUESTIONS and results["false_votes"] >= ELIMINATE_FALSE_VOTES:
        record["closed_reason"] = "eliminated"
        state.eliminated = True
        state.eliminate_reason = (
            results["reason"] or f"{results['false_votes']} 位评委认为与岗位不匹配。"
        )
        state.closed_by = "eliminated"
        state.awaiting_answer = False
        state.current_question = ""
        state.current_question_meta = {}
        state.phase = "finished"
        state.error = warnings
        generate_report()  # 淘汰即刻出报告，直接进入报告页
        return

    # 2) 问满上限 → 进入反问环节
    if index >= MAX_QUESTIONS:
        record["closed_reason"] = "cap"
        state.closed_by = "cap"
        finish_questions()
        state.error = warnings
        return

    # 3) 无人投 false 且本题最终分达标 → 评委认为问够了，提前收尾
    if (
        index >= MIN_QUESTIONS
        and results["false_votes"] == 0
        and results["score"] is not None
        and results["score"] >= EARLY_STOP_SCORE
    ):
        record["closed_reason"] = "high_score"
        state.closed_by = "high_score"
        finish_questions()
        state.error = warnings
        return

    # 4) 继续下一题（换一位主考官重新走「出题 → 评审投票」流程）
    if ask_next_question():
        state.phase = "questions"
    else:
        state.awaiting_answer = False
        state.current_question = ""
        state.current_question_meta = {}
        state.phase = "finished"
    if warnings:
        state.error = f"{state.error}；{warnings}" if state.error else warnings


def reverse_judge_order() -> list[str]:
    """反问环节由本场最后一道题的主考官作答（失败时依次换人）。"""
    state = st.session_state
    if state.qa_history:
        writer = state.qa_history[-1].get("writer")
        if writer in JUDGES:
            return [writer] + [name for name in judge_preference() if name != writer]
    return judge_preference()


def submit_reverse_question(question: str) -> None:
    """反问环节：面试官（最后一位主考官）回答候选人提出的问题。"""
    state = st.session_state
    context = interview_context()
    judge, raw, err = call_with_fallback(
        reverse_judge_order(),
        lambda name: build_reverse_messages(name, question, context),
        json_mode=False,
        temperature=0.8,
        max_tokens=TEXT_JUDGE_MAX_TOKENS,
    )
    if err:
        state.error = err
        return
    answer = clean_reverse_answer(raw)
    state.reverse_history.append({"question": question, "answer": answer, "judge": judge})
    state.error = ""


def fetch_input_check(job: str, company: str) -> tuple[dict[str, dict[str, Any]] | None, str]:
    """调一次 DeepSeek 校验岗位 / 公司是否真实存在，并拿回 3 个真实候选。

    返回 ({"job": {...}, "company": {...}}, 错误)。失败或输出不可解析时返回
    (None, 错误原文)，由页面用黄色警告拦住，绝不让未经校验的输入开始面试。
    """
    raw, err = call_one(
        VERIFY_JUDGE,
        build_verify_messages(job, company),
        json_mode=True,
        temperature=0.0,
        max_tokens=VERIFY_MAX_TOKENS,
    )
    if err or not raw.strip():
        return None, err or f"{judge_label(VERIFY_JUDGE)} 没有返回校验结果。"
    parsed = parse_input_check(raw, company)
    if parsed is None:
        return None, f"{judge_label(VERIFY_JUDGE)} 返回的校验结果无法解析。"
    return parsed, ""


def fetch_resume_check(resume_text: str) -> tuple[dict[str, Any] | None, str]:
    """调一次 DeepSeek 判断上传的文件是不是有效简历。

    返回 ({"is_resume": bool | None, "reason": str}, 错误)；失败时返回 (None, 错误)。
    """
    raw, err = call_one(
        VERIFY_JUDGE,
        build_resume_check_messages(resume_text),
        json_mode=True,
        temperature=0.0,
        max_tokens=RESUME_CHECK_MAX_TOKENS,
    )
    if err or not raw.strip():
        return None, err or f"{judge_label(VERIFY_JUDGE)} 没有返回简历校验结果。"
    parsed = parse_resume_check(raw)
    if parsed is None:
        return None, f"{judge_label(VERIFY_JUDGE)} 返回的简历校验结果无法解析。"
    return parsed, ""


def fetch_suggestions(job: str, company: str) -> tuple[dict[str, list[str]], str]:
    """兼容旧名字：只取岗位 / 公司候选（校验状态由 fetch_input_check 提供）。"""
    check, err = fetch_input_check(job, company)
    if not check:
        return {"jobs": [], "companies": []}, err
    return {
        "jobs": list((check.get("job") or {}).get("suggestions") or []),
        "companies": list((check.get("company") or {}).get("suggestions") or []),
    }, ""


def fetch_resume_match() -> dict[str, Any] | None:
    """有简历时让一位评委给 0-100 的匹配度；失败返回 None，不阻塞报告。"""
    state = st.session_state
    if not state.resume_text:
        return None
    context = interview_context()
    transcript = build_transcript(state)
    judge, raw, _err = call_with_fallback(
        judge_preference(),
        lambda name: build_resume_match_messages(name, context, transcript),
        json_mode=True,
        max_tokens=SINGLE_JUDGE_MAX_TOKENS,
    )
    if not raw:
        return None
    return parse_resume_match(raw)


# --------------------------------------------------------------------------- #
# 汇总统计
# --------------------------------------------------------------------------- #
CLOSED_BY_TEXT: dict[str, str] = {
    "eliminated": "被提前淘汰（与岗位不匹配）",
    "high_score": "评委一致高分，提前收尾（未问满上限）",
    "cap": f"问满 {MAX_QUESTIONS} 题上限",
    "manual": "候选人主动结束",
}


def overall_average(state: Any) -> float | None:
    """本场最终平均分：各题的「去极值平均」再取平均。"""
    scores = [item["score"] for item in state.qa_history if item.get("score") is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


def state_dimension_averages(state: Any) -> dict[str, float]:
    """全场所有评委、所有题目的维度平均分（每项按自己的满分）。"""
    return average_dimensions(
        result.get("dimensions", {})
        for item in state.qa_history
        for result in item.get("judge_results", [])
        if result.get("score") is not None
    )


def dimension_level(key: str, value: float) -> str:
    """把维度得分率映射成「强 / 中 / 弱」。"""
    max_score = float(SCORE_DIMENSIONS[key]["max"])
    ratio = value / max_score if max_score else 0.0
    if ratio >= 0.8:
        return "强"
    if ratio >= 0.6:
        return "中"
    return "弱"


def lowest_question_for_dimension(state: Any, key: str) -> dict[str, Any] | None:
    """找出该维度平均分最低的一道题；用于给出「第 N 题具体哪里弱」的建议。"""
    candidates: list[tuple[float, int, str, str]] = []
    for item in state.qa_history:
        values = []
        for result in item.get("judge_results", []):
            dims = result.get("dimensions") or {}
            if isinstance(dims, dict) and isinstance(dims.get(key), (int, float)):
                values.append(float(dims[key]))
        if values:
            candidates.append(
                (
                    sum(values) / len(values),
                    int(item.get("index") or 0),
                    str(item.get("question") or ""),
                    f"{item.get('question') or ''} {item.get('answer') or ''}",
                )
            )
    if not candidates:
        return None
    average, index, question, search_text = min(candidates, key=lambda item: item[0])
    return {
        "average": round(average, 1),
        "index": index,
        "question": question,
        "search_text": search_text,
    }


def topic_advice_for_question(track: dict[str, Any], text: str) -> str:
    """按具体题目关键词匹配学习建议（例如 JVM 内存模型 → 《深入理解 Java 虚拟机》第 2 章）。"""
    lowered = (text or "").lower()
    for keywords, advice in JOB_TRACK_TOPIC_ADVICE.get(track.get("key", ""), []):
        if any(keyword.lower() in lowered for keyword in keywords):
            return advice
    return ""


def shorten_text(text: str, limit: int = 32) -> str:
    """报告里引用题目时截短，避免一行太长。"""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"


def build_improvement_suggestions(state: Any, limit: int = 3) -> list[str]:
    """按维度短板生成具体改进建议（带题目定位与学习材料），不写空泛套话。"""
    if not state.qa_history:
        return []
    track = detect_job_track(state.job)
    averages = state_dimension_averages(state)
    ranked = sorted(
        DIMENSION_ORDER,
        key=lambda key: averages.get(key, 0.0) / float(SCORE_DIMENSIONS[key]["max"]),
    )
    suggestions: list[str] = []
    for key in ranked[:limit]:
        meta = SCORE_DIMENSIONS[key]
        average = averages.get(key, 0.0)
        level = dimension_level(key, average)
        prefix = f"{meta['label']} {average:.1f}/{meta['max']}（{level}）"
        lowest = lowest_question_for_dimension(state, key)
        if level == "强":
            prefix += "，各维度相对均衡"
        else:
            prefix += "，是当前短板"
        if lowest:
            prefix += (
                f"；最低的是第 {lowest['index']} 题「{shorten_text(lowest['question'])}」"
                f"（{lowest['average']:.1f}/{meta['max']}）"
            )
        if key == "technical_depth":
            advice = topic_advice_for_question(track, lowest["search_text"] if lowest else "")
            if not advice:
                advice = str(track["advice"]["technical_depth"])
        else:
            advice = DIMENSION_ADVICE[key]
        suggestions.append(f"{prefix}。建议：{advice}")
    return suggestions


def record_interview_if_needed() -> None:
    """每场面试只写一次 SQLite 历史记录（重新生成报告不会重复插入）。"""
    state = st.session_state
    if state.history_saved:
        return
    try:
        record_id = save_interview(
            job=state.job,
            company=state.company,
            mode=state.mode,
            score=overall_average(state),
            questions_count=len(state.qa_history),
        )
    except Exception as exc:  # noqa: BLE001 —— 本地数据库异常不影响报告页
        state.history_error = f"历史记录保存失败（interview.db）：{exc}"
        return
    state.history_saved = True
    state.history_record_id = record_id
    state.history_error = ""


def judge_score_matrix(state: Any) -> list[dict[str, Any]]:
    """每位评委的独立评分序列与平均分（只列本场真正参与打分的评委）。"""
    seen: list[str] = []
    for item in state.qa_history:
        for result in item.get("judge_results", []):
            if result["judge"] not in seen:
                seen.append(result["judge"])
    matrix: list[dict[str, Any]] = []
    for name in seen or score_judges():
        scores: list[int] = []
        for item in state.qa_history:
            for result in item.get("judge_results", []):
                if result["judge"] == name and result["score"] is not None:
                    scores.append(result["score"])
        matrix.append(
            {
                "judge": name,
                "label": judge_label(name),
                "scores": scores,
                "average": round(sum(scores) / len(scores), 1) if scores else None,
            }
        )
    return matrix


def build_facts_block(state: Any) -> str:
    """给主审（AI 综合点评）的客观数据。"""
    asked = len(state.qa_history)
    final = overall_average(state)
    scores = [item["score"] for item in state.qa_history if item.get("score") is not None]
    track = detect_job_track(state.job)
    dimensions = state_dimension_averages(state)
    lines = [
        f"- 应聘岗位：{state.job}（岗位方向：{track['label']}）",
        f"- 面试模式：{state.mode}；公司规模：{state.scale}；目标企业：{state.company or '未填写'}",
        f"- 简历：{state.resume_name if state.resume_text else '未上传'}",
        f"- 实际问答轮数：{asked} 题（规则：最少 {MIN_QUESTIONS} 题、最多 {MAX_QUESTIONS} 题）",
        f"- 本场最终平均分（各题去极值后取平均）：{final if final is not None else '无有效评分'}",
        f"- 单题最高分：{max(scores) if scores else '无'}；单题最低分：{min(scores) if scores else '无'}",
        f"- 是否提前淘汰：{'是' if state.eliminated else '否'}",
    ]
    lines.append("- 能力维度平均分（满分：技术深度 3 / 项目经验 3 / 沟通表达 2 / 逻辑思维 2）：")
    for key, meta in SCORE_DIMENSIONS.items():
        value = dimensions.get(key, 0.0)
        ratio = value / meta["max"] if meta["max"] else 0.0
        lines.append(
            f"  - {meta['label']}：{value:.1f}/{meta['max']}"
            f"（得分率 {ratio:.0%}，{dimension_level(key, value)}）"
        )
    ranked = sorted(
        DIMENSION_ORDER,
        key=lambda key: dimensions.get(key, 0.0) / float(SCORE_DIMENSIONS[key]["max"]),
    )
    lines.append(
        "- 维度短板排序（从弱到强）："
        + " → ".join(
            f"{SCORE_DIMENSIONS[key]['label']}({dimensions.get(key, 0.0):.1f}/{SCORE_DIMENSIONS[key]['max']})"
            for key in ranked
        )
    )
    if state.eliminated:
        lines.append(f"- 淘汰发生在第 {asked} 题，淘汰依据：{state.eliminate_reason}")
    else:
        lines.append(f"- 问答结束原因：{CLOSED_BY_TEXT.get(state.closed_by, '未知')}")
    lines.append(f"- 反问环节候选人提问：{len(state.reverse_history)} 个")
    if state.resume_match:
        lines.append(
            f"- 简历匹配度：{state.resume_match['score']} / 100（{state.resume_match['comment']}）"
        )
    elif state.resume_text:
        lines.append("- 简历匹配度：已上传简历但未能评分")
    else:
        lines.append("- 简历匹配度：候选人未上传简历，未评估")
    for row in judge_score_matrix(state):
        lines.append(
            f"- 评委「{row['label']}」独立评分：{row['scores'] or '无'}，平均 {row['average']}"
        )
    return "\n".join(lines)


def build_records_block(state: Any) -> str:
    """给主审（AI 综合点评）的逐题明细：主考官、评审投票、6 位评委评语。"""
    blocks: list[str] = []
    for item in state.qa_history:
        lines = [
            f"### 第 {item['index']} 题（主考官：{judge_label(item.get('writer', ''))}）",
            f"题目：{item['question']}",
        ]
        if item.get("rewrites"):
            outcome = "通过评审" if item.get("passed") else "仍未通过，已回退主考官原题"
            lines.append(f"评审过程：原题被评审团否决，经 {item['rewrites']} 次重写后{outcome}")
        for rnd in item.get("rounds", []):
            votes = "；".join(
                f"{judge_label(vote['judge'])}="
                f"{'通过' if vote['pass'] is True else ('不通过' if vote['pass'] is False else '弃权')}"
                f"{'（' + vote['reason'] + '）' if vote['reason'] else ''}"
                for vote in rnd["votes"]
            )
            lines.append(f"- 第 {rnd['round']} 轮评审（{rnd['passes']}/{rnd['total']} 票通过）：{votes}")
        last_round = (item.get("rounds") or [{}])[-1]
        rejected = [
            f"{judge_label(vote['judge'])}：{vote['reason']}"
            for vote in last_round.get("votes", [])
            if vote["pass"] is False and vote["reason"]
        ]
        if rejected:
            lines.append("- 评审团反对意见：" + "；".join(rejected))
        lines.append(f"候选人回答：{item['answer']}")
        lines.append(f"本题最终分（去极值平均）：{item.get('score')}")
        lines.append(f"本题维度平均：{dimension_summary(item.get('dimensions'))}")
        for result in item.get("judge_results", []):
            lines.append(
                f"- {judge_label(result['judge'])}："
                f"{result['score'] if result['score'] is not None else '未评分'} 分；"
                f"{dimension_summary(result.get('dimensions'))}；"
                f"continue={result['continue']}；点评：{result['comment'] or '—'}"
            )
        blocks.append("\n".join(lines))
    if state.reverse_history:
        blocks.append("### 反问环节")
        for order, item in enumerate(state.reverse_history, start=1):
            blocks.append(f"Q{order}：{item['question']} → {item['answer']}")
    return "\n\n".join(blocks) or "（本场没有问答记录）"


def generate_report() -> bool:
    """准备报告页数据：简历匹配度（有简历时）+ AI 综合点评。

    无论 AI 综合点评是否成功，都会进入报告页（结构化数据始终可用）。
    """
    state = st.session_state
    if state.resume_text and state.resume_match is None:
        state.resume_match = fetch_resume_match()

    facts = build_facts_block(state)
    records = build_records_block(state)
    judge, raw, err = call_with_fallback(
        judge_preference(),
        lambda name: build_summary_messages(name, facts, records),
        json_mode=False,
        temperature=0.7,
        max_tokens=TEXT_JUDGE_MAX_TOKENS,
    )
    state.report = clean_reverse_answer(raw) if raw else ""
    if state.report:
        state.report_error = ""
    else:
        state.report_error = err or "AI 综合点评生成失败，可点击「重新生成 AI 点评」重试。"
    state.stage = "report"
    state.phase = "finished"
    state.awaiting_answer = False
    state.current_question_meta = {}
    state.confirm_end = False
    record_interview_if_needed()
    return bool(state.report)


# --------------------------------------------------------------------------- #
# Markdown 导出
# --------------------------------------------------------------------------- #
def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    """生成 Markdown 表格（自动转义竖线与换行）。"""

    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").strip()

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(cell(item) for item in row) + " |")
    return "\n".join(lines)


def vote_text(pass_flag: bool | None) -> str:
    """评审投票结论的展示文案。"""
    if pass_flag is True:
        return "✅ 通过"
    if pass_flag is False:
        return "❌ 不通过"
    return "— 弃权"


def continue_text(flag: bool | None) -> str:
    """continue 字段的展示文案。"""
    if flag is True:
        return "继续"
    if flag is False:
        return "结束"
    return "—"


def build_report_markdown() -> str:
    """把报告页的全部内容（含评审投票与各评委评分）导出为 Markdown。"""
    state = st.session_state
    final = overall_average(state)
    lines = [
        f"# 模拟面试总结报告（{architecture_label()}）",
        "",
        f"- 应聘岗位：{state.job}（岗位方向：{detect_job_track(state.job)['label']}）",
        f"- 面试模式：{state.mode}",
        f"- 公司规模：{state.scale}",
        f"- 目标企业：{state.company or '未填写'}",
        f"- 简历参考：{state.resume_name if state.resume_text else '未上传'}",
        f"- 实际问答轮数：{len(state.qa_history)} 题（规则：最少 {MIN_QUESTIONS} 题、最多 {MAX_QUESTIONS} 题）",
        f"- 本场最终平均分：{final if final is not None else '无有效评分'}",
        f"- 是否提前淘汰：{'是' if state.eliminated else '否'}",
    ]
    if state.eliminated:
        lines.append(f"- 淘汰依据：{state.eliminate_reason}")
    else:
        lines.append(f"- 问答结束原因：{CLOSED_BY_TEXT.get(state.closed_by, '未知')}")
    if state.resume_text:
        if state.resume_match:
            lines.append(
                f"- 简历匹配度评分：{state.resume_match['score']} / 100"
                f"（{state.resume_match['comment']}）"
            )
        else:
            lines.append("- 简历匹配度评分：未能生成")

    dimensions = state_dimension_averages(state)
    lines += [
        "",
        "## 能力维度分析",
        "",
        markdown_table(
            ["维度", "平均分", "满分", "得分率", "评价", "考察点"],
            [
                [
                    meta["label"],
                    f"{dimensions.get(key, 0.0):.1f}",
                    meta["max"],
                    f"{dimensions.get(key, 0.0) / meta['max']:.0%}" if meta["max"] else "0%",
                    dimension_level(key, dimensions.get(key, 0.0)),
                    meta["desc"],
                ]
                for key, meta in SCORE_DIMENSIONS.items()
            ],
        ),
        "",
    ]
    suggestions = build_improvement_suggestions(state)
    if suggestions:
        lines += ["## 改进建议", ""]
        lines += [f"- {suggestion}" for suggestion in suggestions]
        lines.append("")

    for item in state.qa_history:
        lines += [
            "",
            f"## 第 {item['index']} 题（主考官：{judge_label(item.get('writer', ''))}）",
            "",
            f"**题目：** {item['question']}",
            "",
        ]
        if item.get("rewrites"):
            outcome = "通过评审" if item.get("passed") else "仍未通过，已回退主考官原题"
            lines += [f"> 评审过程：原题被评审团否决，经 {item['rewrites']} 次重写后{outcome}", ""]
        for rnd in item.get("rounds", []):
            lines += [
                f"**第 {rnd['round']} 轮评审投票（{rnd['passes']}/{rnd['total']} 票通过）**",
                "",
                markdown_table(
                    ["评审", "结论", "理由"],
                    [
                        [
                            judge_label(vote["judge"]),
                            vote_text(vote["pass"]),
                            vote["reason"] or vote["error"] or "—",
                        ]
                        for vote in rnd["votes"]
                    ],
                ),
                "",
            ]
        lines += [
            f"**你的回答：** {item['answer']}",
            "",
            markdown_table(
                ["评委", "独立分数", "维度分", "是否继续", "评语"],
                [
                    [
                        judge_label(result["judge"]),
                        result["score"] if result["score"] is not None else "—",
                        dimension_summary(result.get("dimensions")),
                        continue_text(result["continue"]),
                        result["comment"] or result["error"] or "—",
                    ]
                    for result in item.get("judge_results", [])
                ],
            ),
            "",
            f"**本题维度平均：** {dimension_summary(item.get('dimensions'))}",
            "",
            "**本题最终分（去掉一个最高分和一个最低分后取平均）："
            f"{item['score'] if item.get('score') is not None else '—'} / 10**",
            "",
        ]

    lines += [
        f"## {len(judge_score_matrix(state))} 位评委独立评分总览",
        "",
        markdown_table(
            ["评委", "各题分数", "平均分"],
            [
                [
                    row["label"],
                    "、".join(str(score) for score in row["scores"]) or "—",
                    row["average"] if row["average"] is not None else "—",
                ]
                for row in judge_score_matrix(state)
            ],
        ),
        "",
    ]

    if state.reverse_history:
        lines += ["## 反问环节", ""]
        for order, item in enumerate(state.reverse_history, start=1):
            lines += [
                f"**Q{order}（{judge_label(item.get('judge', ''))}）：** {item['question']}",
                "",
                item["answer"],
                "",
            ]

    lines += ["", "## AI 综合点评", "", state.report or (state.report_error or "（尚未生成）")]
    return "\n".join(lines)


def safe_filename(name: str) -> str:
    """把岗位名清洗成可安全用作文件名的字符串。"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")
    return cleaned or "面试报告"


# --------------------------------------------------------------------------- #
# 页面渲染
# --------------------------------------------------------------------------- #
def run_judge_health_check() -> dict[str, dict[str, Any]]:
    """并发自检 6 位评委的连通性（每位一次极小的请求）。"""
    results = call_many(
        JUDGE_ORDER,
        lambda name: [
            {"role": "system", "content": f"你是「{judge_label(name)}」评委。"},
            {"role": "user", "content": "请只回复两个字：正常"},
        ],
        json_mode=False,
        temperature=0.0,
        max_tokens=16,
    )
    health: dict[str, dict[str, Any]] = {}
    for name in JUDGE_ORDER:
        text, err = results.get(name, ("", "未知错误"))
        health[name] = {
            "ok": (not err) and bool(text.strip()),
            "model": current_model(name),
            "detail": text.strip()[:20] if not err else err,
        }
    return health


def render_judge_roster() -> None:
    """展示 6 位评委的模型与人设。"""
    st.markdown(
        markdown_table(
            ["评委", "模型", "人设 / 关注点"],
            [
                [JUDGE_META[name]["label"], JUDGES[name]["model"], JUDGE_META[name]["persona"]]
                for name in JUDGE_ORDER
            ],
        )
    )


def render_judge_health(health: dict[str, dict[str, Any]]) -> None:
    """展示自检结果。"""
    if not health:
        st.caption(
            "还没有自检过。点击下面的按钮会并发调用 6 家接口各一次（token 消耗极小），"
            "用来确认 6 个 Key / 模型是否可用。"
        )
        return
    st.markdown(
        markdown_table(
            ["评委", "当前模型", "状态", "返回 / 错误"],
            [
                [
                    JUDGE_META[name]["label"],
                    (health.get(name) or {}).get("model", JUDGES[name]["model"]),
                    "✅ 正常" if (health.get(name) or {}).get("ok") else "❌ 失败",
                    (health.get(name) or {}).get("detail", ""),
                ]
                for name in JUDGE_ORDER
            ],
        )
    )
    if any(not (health.get(name) or {}).get("ok") for name in JUDGE_ORDER):
        st.warning(
            "有评委调用失败：如果是豆包报「模型不存在 / 无权限」，"
            "把 JUDGE_META['doubao']['endpoint_id'] 填成火山方舟的接入点 ID（ep-xxx）即可，"
            "代码会自动用它重试并缓存生效的模型。"
            "如果是文心（千帆）报模型不存在 / 账户欠费，代码会从 ernie-5.1 自动尝试 ernie-5.0，"
            "两个都不通就去千帆控制台确认余额 / 已开通的模型名。"
        )


def usable_suggestions(info: dict[str, Any], raw: str, status: str) -> list[str]:
    """取本次可选的候选；非 valid 时把用户原文从候选里剔除，避免假输入被选回来。"""
    items = [
        item
        for item in clean_items(info.get("suggestions") or [], VERIFY_ITEM_LIMIT)
        if item != VERIFY_NONE_LABEL
    ]
    if status != VERIFY_VALID and raw.strip():
        items = [item for item in items if item.strip() != raw.strip()]
    return items


def verification_issue_text(info: dict[str, Any], raw: str, label: str) -> str:
    """把单个字段的校验结果翻译成黄色警告里的一句话。"""
    status = normalize_verify_status(info.get("status"))
    reason = str(info.get("reason") or "").strip()
    reason_text = f"（{reason}）" if reason else ""
    if status == VERIFY_INVALID:
        return f"{label}「{raw}」不是真实存在的{label}{reason_text}"
    if status == VERIFY_UNCERTAIN:
        return f"没能确认{label}「{raw}」是否真实存在{reason_text}"
    return ""


def render_suggest() -> None:
    """真实性校验结果确认页：只允许选真实岗位 / 公司，然后才开始面试。"""
    state = st.session_state
    pending = state.pending_setup or {}
    if not pending:
        state.stage = "setup"
        st.rerun()
        return

    raw_job = str(pending.get("job") or "").strip()
    raw_company = str(pending.get("company") or "").strip()
    check = state.input_check or {}
    job_info = check.get("job") or {}
    company_info = check.get("company") or {}
    job_status = normalize_verify_status(job_info.get("status"))
    if job_status == VERIFY_EMPTY:
        job_status = VERIFY_UNCERTAIN
    company_status = (
        normalize_verify_status(company_info.get("status"))
        if raw_company
        else VERIFY_EMPTY
    )
    if raw_company and company_status == VERIFY_EMPTY:
        company_status = VERIFY_UNCERTAIN
    job_options = usable_suggestions(job_info, raw_job, job_status)
    company_options = (
        usable_suggestions(company_info, raw_company, company_status) if raw_company else []
    )
    signature = suggestion_signature(raw_job, raw_company)

    st.title("🧾 真实性校验：先确认真实岗位和公司")
    st.caption(
        "DeepSeek 已经判断了你填写的岗位 / 公司是否真实存在，并给出最接近的真实候选。"
        "只有确认真实的信息才能开始面试，虚构岗位或公司不会被放行。"
    )
    raw_line = f"**你输入的是**：岗位 `{raw_job}`"
    raw_line += f"，公司 `{raw_company}`" if raw_company else "（公司未填，本场不指定企业）"
    st.markdown(raw_line)
    track = detect_job_track(raw_job)
    st.caption(f"🎯 识别岗位方向：{track['label']}；本场出题范围：{track['focus']}")

    if pending.get("resume_text"):
        resume_reason = str((state.resume_check or {}).get("reason") or "").strip()
        st.success(
            f"简历校验通过：已识别为有效简历。{resume_reason}" if resume_reason else "简历校验通过：已识别为有效简历。"
        )
    else:
        st.caption("未上传简历，本场不做简历参考。")

    issues: list[str] = []
    job_issue = verification_issue_text(job_info, raw_job, "岗位")
    if job_issue:
        issues.append(job_issue)
    if raw_company:
        company_issue = verification_issue_text(company_info, raw_company, "公司")
        if company_issue:
            issues.append(company_issue)
    if issues:
        st.warning(
            f"{VERIFY_FAIL_MESSAGE}：{'；'.join(issues)}。"
            "请从下面的真实选项中选择，或返回修改输入。"
        )
    else:
        st.success("岗位和公司真实性校验通过。")

    if job_status != VERIFY_VALID and not job_options:
        st.warning(
            f"{VERIFY_FAIL_MESSAGE}：岗位「{raw_job}」没有找到可替换的真实岗位，请返回修改输入。"
        )
        if st.button("↩️ 返回修改输入", use_container_width=True):
            state.stage = "setup"
            state.pending_setup = {}
            state.input_check = {}
            state.resume_check = {}
            st.rerun()
        return
    if raw_company and company_status != VERIFY_VALID and not company_options:
        st.warning(
            f"{VERIFY_FAIL_MESSAGE}：公司「{raw_company}」没有找到可替换的真实公司，请返回修改输入。"
        )
        if st.button("↩️ 返回修改输入", use_container_width=True):
            state.stage = "setup"
            state.pending_setup = {}
            state.input_check = {}
            state.resume_check = {}
            st.rerun()
        return

    if job_status == VERIFY_VALID:
        job_choices = job_options + [VERIFY_NONE_LABEL]
        job_label = "岗位：确认要面试的岗位（也可以选 DeepSeek 纠正后的真实岗位）"
    else:
        job_choices = job_options
        job_label = "岗位：请从下面选择一个真实存在的岗位"
    job_choice = st.radio(job_label, job_choices, key=f"job_pick_{signature}")

    if not raw_company:
        company_choice = ""
        st.caption("公司留空，本场不会用企业口吻提问。")
    elif company_status == VERIFY_VALID:
        company_choices = company_options + [VERIFY_NONE_LABEL]
        company_choice = st.radio(
            "公司：确认要投的公司（也可以选 DeepSeek 纠正后的真实公司）",
            company_choices,
            key=f"company_pick_{signature}",
        )
    else:
        company_choice = st.radio(
            "公司：请从下面选择一家真实存在的公司",
            company_options,
            key=f"company_pick_{signature}",
        )

    col_confirm, col_back = st.columns(2)
    with col_confirm:
        confirmed = st.button("✅ 确认并开始面试", type="primary", use_container_width=True)
    with col_back:
        back = st.button("↩️ 返回修改输入", use_container_width=True)

    if back:
        state.stage = "setup"
        state.pending_setup = {}
        state.input_check = {}
        state.resume_check = {}
        st.rerun()
        return
    if not confirmed:
        return

    job = raw_job if job_choice == VERIFY_NONE_LABEL else job_choice
    company = raw_company if company_choice == VERIFY_NONE_LABEL else company_choice
    job_trusted = (
        job_choice != VERIFY_NONE_LABEL and job_choice in job_options
    ) or (job_choice == VERIFY_NONE_LABEL and job_status == VERIFY_VALID)
    company_trusted = True
    if raw_company:
        company_trusted = (
            company_choice != VERIFY_NONE_LABEL and company_choice in company_options
        ) or (company_choice == VERIFY_NONE_LABEL and company_status == VERIFY_VALID)
    if not job_trusted or not company_trusted:
        st.warning(f"{VERIFY_FAIL_MESSAGE}：请从校验通过的真实选项中选择后再开始面试。")
        return

    quick = bool(pending.get("quick_mode"))
    with st.spinner(
        "GLM 正在出题（快速模式）……"
        if quick
        else f"评委团（{len(FAST_JUDGES)} 个快模型出题 + {REVIEW_TEAM_SIZE} 位评审投票）正在出题……"
    ):
        started = start_interview(
            job,
            pending.get("mode", MODE_HR),
            pending.get("scale", SCALE_BIG),
            company,
            pending.get("resume_text", ""),
            pending.get("resume_name", ""),
            quick=quick,
        )
    if not started:
        state.stage = "setup"
        state.error = state.error or "第 1 题没出出来，请检查输入或稍后重试。"
        st.rerun()
        return
    state.pending_setup = {}
    state.suggest = {}
    state.suggest_error = ""
    state.input_check = {}
    state.resume_check = {}
    st.rerun()


def render_hero() -> None:
    """设置页顶部的品牌标题。"""
    st.markdown(
        """
        <div class="hero">
            <h1>面霸 AI</h1>
            <p class="hero-subtitle">6 大模型交叉评审・模拟真实求职面试</p>
            <div class="hero-rule"></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_setup() -> None:
    state = st.session_state

    with st.container(key="setup_card"):
        st.caption("常见岗位快捷选择")
        current_job = str(state.get("job_input", "") or "").strip()
        with st.container(key="quick_jobs"):
            quick_cols = st.columns(len(QUICK_JOB_OPTIONS))
            for index, role in enumerate(QUICK_JOB_OPTIONS):
                selected = current_job == role
                if quick_cols[index].button(
                    role,
                    key=f"quick_job_{index}",
                    type="primary" if selected else "secondary",
                    use_container_width=True,
                ):
                    state.job_input = role
                    st.rerun()

        with st.form("setup_form", border=False):
            job = st.text_input(
                "应聘岗位",
                key="job_input",
                placeholder="例如：Java 后端开发工程师 / 产品经理（校招）",
            )
            mode = st.selectbox(
                "面试模式",
                options=list(INTERVIEW_MODES.keys()),
                format_func=lambda name: f"{name} —— {INTERVIEW_MODES[name]['desc']}",
                key="mode_input",
            )
            scale = st.selectbox(
                "公司规模",
                options=list(COMPANY_SCALES.keys()),
                format_func=lambda name: f"{name} —— {COMPANY_SCALES[name]['desc']}",
                key="scale_input",
            )
            company = st.text_input(
                "目标企业（选填）",
                key="company_input",
                placeholder="例如：字节跳动",
            )
            resume = st.file_uploader(
                "上传简历（PDF，选填）",
                type=["pdf"],
                key="resume_input",
                help="支持 PDF 格式，开始面试前会自动校验是否有效。",
            )
            submitted = st.form_submit_button(
                "开始面试",
                type="primary",
                use_container_width=True,
            )

    quick = bool(state.get("quick_input", False))

    if submitted:
        job = (job or "").strip()
        company = (company or "").strip()
        if not job:
            st.warning("请先填写你要应聘的岗位。")
            return
        resume_name = ""
        resume_text = ""
        resume_check: dict[str, Any] = {}
        if resume is not None:
            resume_name = getattr(resume, "name", "resume.pdf")
            try:
                resume_text = extract_pdf_text(resume.getvalue())
            except ResumeParseError as exc:
                st.warning(f"简历解析失败：{exc}。请换一份可复制文字的 PDF，或清空简历后重试。")
                return
            if not resume_text.strip():
                st.warning(
                    "这份 PDF 没有读取到文字（可能是扫描件 / 图片版），无法校验是否为有效简历。"
                    "请上传可复制文字的 PDF，或清空简历后重试。"
                )
                return
            with st.spinner("正在校验简历是否为有效简历……"):
                resume_check, resume_err = fetch_resume_check(resume_text)
            if resume_err:
                st.warning(f"简历真实性校验暂时不可用（{resume_err}），请稍后重试。")
                return
            if not resume_check:
                st.warning("没能校验这份简历，请稍后重试或换一份 PDF。")
                return
            if resume_check.get("is_resume") is False:
                st.warning(RESUME_INVALID_MESSAGE)
                return
            if resume_check.get("is_resume") is not True:
                st.warning(
                    "没能确认这是一份有效简历，请重新上传包含姓名、教育经历、工作 / 项目经历和技能的简历。"
                )
                return
        with st.spinner("正在校验岗位和公司是否真实存在……"):
            input_check, check_error = fetch_input_check(job, company)
        if check_error:
            st.warning(f"真实性校验暂时不可用（{check_error}），请稍后重试。")
            return
        if not input_check:
            st.warning("没能完成岗位 / 公司真实性校验，请稍后重试。")
            return
        job_info = input_check.get("job") or {}
        company_info = input_check.get("company") or {}
        job_status = normalize_verify_status(job_info.get("status"))
        if job_status == VERIFY_EMPTY:
            job_status = VERIFY_UNCERTAIN
        company_status = (
            normalize_verify_status(company_info.get("status")) if company else VERIFY_EMPTY
        )
        if company and company_status == VERIFY_EMPTY:
            company_status = VERIFY_UNCERTAIN
        job_options = usable_suggestions(job_info, job, job_status)
        company_options = (
            usable_suggestions(company_info, company, company_status) if company else []
        )
        if job_status != VERIFY_VALID and not job_options:
            st.warning(
                f"{VERIFY_FAIL_MESSAGE}：岗位「{job}」不是可确认的真实岗位，"
                "且没有找到可替换的真实岗位，请修改后重试。"
            )
            return
        if company and company_status != VERIFY_VALID and not company_options:
            st.warning(
                f"{VERIFY_FAIL_MESSAGE}：公司「{company}」不是可确认的真实公司，"
                "且没有找到可替换的真实公司，请修改后重试。"
            )
            return
        st.session_state.update(
            {
                "stage": "confirm",
                "pending_setup": {
                    "job": job,
                    "mode": mode,
                    "scale": scale,
                    "company": company,
                    "resume_text": resume_text,
                    "resume_name": resume_name,
                    "quick_mode": bool(quick),
                },
                "input_check": input_check,
                "resume_check": resume_check,
                "suggest": {
                    "jobs": job_options,
                    "companies": company_options,
                },
                "suggest_error": "",
            }
        )
        st.rerun()
        return


def render_review_rounds(rounds: list[dict[str, Any]]) -> None:
    """展示评审团的投票过程（每轮一张表）。"""
    if not rounds:
        st.caption("（没有评审记录）")
        return
    for rnd in rounds:
        st.markdown(
            f"**第 {rnd['round']} 轮评审（{rnd['passes']}/{rnd['total']} 票通过，题目来源：{rnd['kind']}）**"
        )
        st.markdown(
            markdown_table(
                ["评审", "结论", "理由"],
                [
                    [
                        judge_label(vote["judge"]),
                        vote_text(vote["pass"]),
                        vote["reason"] or vote["error"] or "—",
                    ]
                    for vote in rnd["votes"]
                ],
            )
        )
        if rnd.get("rewriters"):
            names = "、".join(judge_label(name) for name in rnd["rewriters"])
            st.caption(f"↩️ 本轮未通过，由 {names} 重写后进入下一轮评审")


def render_question_writer(state: Any, item: dict[str, Any]) -> None:
    """展示「谁出的题」与评审投票结果（面试页与报告页共用）。"""
    writer = judge_label(item.get("writer", ""))
    caption = f"🧑‍⚖️ 本题主考官：{writer}"
    if item.get("rewrites"):
        outcome = (
            "重写版通过评审"
            if item.get("passed")
            else f"{item.get('rewrites', MAX_REWRITES)} 次重写仍未通过，已回退主考官原题"
        )
        caption += f"　|　原题被评审团否决，重写 {item['rewrites']} 次（{outcome}）"
    else:
        caption += "　|　原题评审团投票通过"
    st.caption(caption)


def render_reverse_history(state: Any) -> None:
    """展示反问环节的历史记录。"""
    for item in state.reverse_history:
        with st.chat_message("user", avatar="🙋"):
            st.markdown(item["question"])
        with st.chat_message("assistant", avatar="🧑‍💼"):
            st.markdown(item["answer"])
        st.caption(f"💬 由「{judge_label(item.get('judge', ''))}」以面试官身份回答")


def render_judge_panel(state: Any) -> dict[str, Any]:
    """右侧评委栏：小头像 + 分数，评语折叠；返回可实时更新的占位符。"""
    judges = score_judges()
    record = state.qa_history[-1] if state.qa_history else None
    results = {r["judge"]: r for r in (record or {}).get("judge_results", [])}
    st.markdown("#### 评委评分")
    slots: dict[str, Any] = {}
    for name in judges:
        with st.container(key=f"judge_card_{name}"):
            avatar_col, info_col = st.columns(
                [1, 5], gap="small", vertical_alignment="center"
            )
            with avatar_col:
                st.image(judge_avatar(name), width=32)
            with info_col:
                st.markdown(f"**{judge_label(name)}**")
                score_ph = st.empty()
                result = results.get(name) or {}
                if result.get("score") is not None:
                    score_ph.markdown(judge_score_value_markdown(result))
                else:
                    score_ph.markdown("**—**")
            with st.expander("查看评语", expanded=False):
                comment_ph = st.empty()
                if result:
                    comment_ph.markdown(judge_detail_markdown(result))
                else:
                    comment_ph.caption("评分完成后显示维度分与评语。")
        slots[name] = {"score": score_ph, "comment": comment_ph}
    with st.container(key="judge_summary"):
        summary_ph = st.empty()
        if record and record.get("score") is not None:
            summary_ph.markdown(
                f"**最新平均分　{record['score']:.1f} / {SCORE_TOTAL_MAX}**"
            )
        else:
            summary_ph.caption("等待评分")
    slots["__summary__"] = summary_ph
    return slots


def render_question_phase(state: Any, answered: int, judge_slots: dict[str, Any] | None = None) -> None:
    """左栏：主考官头像 + 题目气泡 + 回答输入框 + 结束控制。"""
    meta = state.current_question_meta or {}
    writer = meta.get("writer", "")
    with st.container(key="question_row"):
        avatar_col, bubble_col = st.columns([1, 8], gap="small", vertical_alignment="top")
        with avatar_col:
            st.image(judge_avatar(writer), width=64)
        with bubble_col:
            with st.container(key="question_bubble"):
                st.caption(f"第 {answered + 1} 题 · 主考官 {judge_label(writer)}")
                st.markdown(state.current_question)

    answer = st.text_area(
        "你的回答",
        key=f"answer_{answered}",
        height=180,
        placeholder="用自己的话作答，尽量给出具体例子、数据和结论……",
    )
    if st.button("提交回答", type="primary", use_container_width=True):
        if not answer.strip():
            st.warning("请先输入你的回答。")
        else:
            submit_answer(answer.strip(), judge_slots)
            st.rerun()

    with st.expander("提前结束本场面试", expanded=False):
        confirmed = st.checkbox(
            "我确认提前结束本场面试（将基于已答题目生成报告）",
            key=f"confirm_end_{answered}",
        )
        if st.button(
            "提前结束并生成报告",
            disabled=not (confirmed and answered > 0),
            use_container_width=True,
        ):
            state.closed_by = "manual"
            state.awaiting_answer = False
            state.current_question = ""
            state.current_question_meta = {}
            state.phase = "finished"
            with st.spinner("正在生成报告……"):
                generate_report()
            st.rerun()


def render_reverse_phase() -> None:
    """反问环节：候选人向面试官提问。"""
    state = st.session_state
    with st.chat_message("assistant", avatar="🧑‍💼"):
        st.markdown(REVERSE_INTRO)
    st.caption(
        "可以问薪资构成、加班强度、团队氛围、晋升路径……如果没有其他问题，"
        "直接回复「没有了」或点击下方「结束提问并生成报告」。"
    )

    index = len(state.reverse_history)
    with st.form(f"reverse_form_{index}"):
        question = st.text_area(
            "🙋 你想问面试官什么？",
            key=f"reverse_q_{index}",
            height=100,
            placeholder="例如：这个岗位的薪资结构是怎样的？团队加班多吗？入职后晋升路径是什么？",
        )
        submitted = st.form_submit_button("💬 提问", use_container_width=True)

    if submitted:
        text = question.strip()
        if not text:
            st.warning("请先输入你想问的问题，或直接点击「结束提问并生成报告」。")
        elif is_reverse_finished(text):
            with st.spinner("正在生成报告……"):
                generate_report()
            st.rerun()
        else:
            with st.spinner("面试官正在回答你的问题……"):
                submit_reverse_question(text)
            st.rerun()

    st.divider()
    if st.button("🏁 结束提问并生成报告", type="primary", use_container_width=True):
        with st.spinner("正在生成报告……"):
            generate_report()
        st.rerun()


def render_finished_phase() -> None:
    """问答已结束但报告未就绪时的兜底页面（例如出题失败 / 报告生成失败）。"""
    state = st.session_state
    if state.eliminated:
        st.error(
            f"⚠️ {state.eliminate_reason}（第 {len(state.qa_history)} 题后提前结束）"
        )
    else:
        st.success("✅ 面试问答环节已经结束。")
    col1, col2 = st.columns(2)
    if col1.button("📝 生成报告", type="primary", use_container_width=True):
        with st.spinner("正在生成报告……"):
            generate_report()
        st.rerun()
    if col2.button("🔄 重新出下一题", use_container_width=True):
        with st.spinner("评委团正在出题……"):
            if ask_next_question():
                state.phase = "questions"
        st.rerun()


def judge_results_table(judge_results: list[dict[str, Any]]) -> str:
    """6 位评委独立打分表（Markdown），含分维度得分。"""
    return markdown_table(
        ["评委", "独立分数", "维度分", "是否继续", "评语"],
        [
            [
                judge_label(result["judge"]),
                result["score"] if result["score"] is not None else "—",
                dimension_summary(result.get("dimensions")),
                continue_text(result["continue"]),
                result["comment"] or result["error"] or "—",
            ]
            for result in judge_results
        ],
    )


def render_dimension_analysis(state: Any) -> None:
    """报告页：分维度展示强弱，并给出定位到具体题目的改进建议。"""
    dimensions = state_dimension_averages(state)
    st.subheader("📊 能力维度分析：哪块弱")
    rows = []
    for key, meta in SCORE_DIMENSIONS.items():
        value = dimensions.get(key, 0.0)
        ratio = value / meta["max"] if meta["max"] else 0.0
        rows.append(
            [
                meta["label"],
                f"{value:.1f}",
                str(meta["max"]),
                f"{ratio:.0%}",
                dimension_level(key, value),
                meta["desc"],
            ]
        )
    st.markdown(
        markdown_table(["维度", "平均分", "满分", "得分率", "评价", "考察点"], rows)
    )
    ranked = sorted(
        DIMENSION_ORDER,
        key=lambda key: dimensions.get(key, 0.0) / float(SCORE_DIMENSIONS[key]["max"]),
    )
    weakest = ranked[0] if ranked else None
    strongest = ranked[-1] if ranked else None
    if weakest and strongest:
        weak_meta = SCORE_DIMENSIONS[weakest]
        strong_meta = SCORE_DIMENSIONS[strongest]
        st.warning(
            f"最弱维度：{weak_meta['label']} {dimensions.get(weakest, 0.0):.1f}/{weak_meta['max']}"
            f"（{dimension_level(weakest, dimensions.get(weakest, 0.0))}）　｜　"
            f"最强维度：{strong_meta['label']} {dimensions.get(strongest, 0.0):.1f}/{strong_meta['max']}"
        )

    st.subheader("🎯 改进建议（按维度短板定位到具体题目）")
    suggestions = build_improvement_suggestions(state)
    if suggestions:
        for order, suggestion in enumerate(suggestions, start=1):
            st.markdown(f"**{order}.** {suggestion}")
    else:
        st.caption("本场没有足够评分，暂时无法生成针对性建议。")


def render_qa_history(state: Any) -> None:
    """历史题目与回答（放在折叠区，避免打断当前题目的左右布局）。"""
    for item in state.qa_history:
        with st.chat_message("assistant", avatar=judge_avatar(item.get("writer", ""))):
            st.markdown(
                f"**第 {item['index']} 题（主考官：{judge_label(item.get('writer', ''))}）：**"
                f" {item['question']}"
            )
        with st.chat_message("user", avatar="🙋"):
            st.markdown(item["answer"])
        render_question_writer(state, item)
        score_text = f"{item['score']:.1f}" if item.get("score") is not None else "—"
        judge_n = len(item.get("judge_results", []))
        st.caption(
            f"本题最终分：{score_text} / 10（{judge_n} 位评委去极值平均）　|　"
            f"维度：{dimension_summary(item.get('dimensions'))}　|　"
            f"投「结束」的评委：{item.get('false_votes', 0)} 位"
        )
        with st.expander(f"第 {item['index']} 题明细：{judge_n} 位评委打分 + 评审投票"):
            st.markdown(f"**{judge_n} 位评委独立打分与评语**")
            st.markdown(judge_results_table(item.get("judge_results", [])))
            st.markdown("**出题时的评审投票**")
            render_review_rounds(item.get("rounds", []))


def render_interview() -> None:
    state = st.session_state
    answered = len(state.qa_history)

    # 极简顶栏
    st.markdown("#### 面霸 AI")
    meta_line = f"{state.job} · {state.mode} · {state.scale}"
    if state.company:
        meta_line += f" · {state.company}"
    if state.resume_text:
        meta_line += " · 简历已上传"
    st.caption(meta_line)

    left, right = st.columns([7, 3], gap="large")

    # 右侧先创建占位符，左侧提交回答时可以直接实时刷新
    with right:
        judge_slots = render_judge_panel(state)

    with left:
        if state.awaiting_answer and state.current_question:
            render_question_phase(state, answered, judge_slots)
        elif state.phase == "reverse":
            render_reverse_phase()
        elif state.phase == "finished":
            render_finished_phase()
        st.progress(
            min(answered, MAX_QUESTIONS) / MAX_QUESTIONS,
            text=f"进度 {answered} / {MAX_QUESTIONS}",
        )

    if state.qa_history:
        with st.expander(f"历史题目与回答（{len(state.qa_history)} 题）", expanded=False):
            render_qa_history(state)
    if state.reverse_history:
        with st.expander(f"反问记录（{len(state.reverse_history)} 条）", expanded=False):
            render_reverse_history(state)


def render_report() -> None:
    state = st.session_state
    asked = len(state.qa_history)
    final = overall_average(state)
    scores = [item["score"] for item in state.qa_history if item.get("score") is not None]

    st.title("📄 面试总结报告（6 评委版）")
    caption = f"{state.job} · {detect_job_track(state.job)['label']} · {state.mode} · {state.scale}"
    if state.company:
        caption += f" · {state.company}"
    caption += f" · 简历：{state.resume_name}" if state.resume_text else " · 未上传简历"
    st.caption(caption)
    if state.history_saved:
        st.caption("💾 本场已保存到本地历史记录（interview.db）")
    elif state.history_error:
        st.warning(state.history_error)

    # ---- 最终平均分高亮 ----
    if not state.resume_text:
        resume_metric = "未评估"
    elif state.resume_match:
        resume_metric = f"{state.resume_match['score']} / 100"
    else:
        resume_metric = "—"
    final_text = f"{final:.1f}" if final is not None else "—"
    end_text = "被提前淘汰" if state.eliminated else CLOSED_BY_TEXT.get(state.closed_by, "已结束")
    st.markdown(
        '<div style="background:#111418;border:1px solid #23272f;'
        'padding:18px 24px;border-radius:12px;color:#e6e8eb;">'
        '<div style="font-size:13px;color:#8b929e;">本场最终平均分'
        "（各题去掉一个最高分和一个最低分后取平均）</div>"
        f'<div style="font-size:44px;font-weight:700;line-height:1.15;color:#f5f6f7;">{final_text}'
        '<span style="font-size:18px;font-weight:500;color:#8b929e;"> / 10</span></div>'
        f'<div style="font-size:13px;color:#8b929e;">{asked} 题　·　{end_text}　·　'
        f"简历匹配度 {resume_metric}</div></div>",
        unsafe_allow_html=True,
    )

    if state.eliminated:
        st.error(f"⚠️ 本场被提前淘汰（第 {asked} 题）：{state.eliminate_reason}")
    else:
        st.success(
            f"✅ 本场问答结束：{end_text}，共 {asked} 题"
            f"（规则：最少 {MIN_QUESTIONS} 题、最多 {MAX_QUESTIONS} 题）"
        )

    col1, col2, col3 = st.columns(3)
    col1.metric("实际问答题数", f"{asked} 题")
    col2.metric("最终平均分", final_text)
    col3.metric("单题最高分", f"{max(scores)} 分" if scores else "—")

    col4, col5 = st.columns(2)
    col4.metric("是否提前淘汰", "是" if state.eliminated else "否")
    col5.metric("简历匹配度", resume_metric)
    if state.resume_text and state.resume_match and state.resume_match["comment"]:
        st.caption(f"📎 简历匹配度点评：{state.resume_match['comment']}")
    elif not state.resume_text:
        st.caption("📎 本次未上传简历，因此没有简历匹配度评分。")
    if state.reverse_history:
        st.caption(f"🙋 反问环节：你共提问 {len(state.reverse_history)} 个问题")

    st.divider()
    render_dimension_analysis(state)
    st.divider()
    panel_n = max([review_size(item) for item in state.qa_history] or [0])
    judge_n = max([len(item.get("judge_results", [])) for item in state.qa_history] or [0])
    review_text = f"{panel_n} 位评审怎么投票" if panel_n else "快速模式没有独立评审团"
    st.subheader(f"🧑‍⚖️ 逐题明细：谁出的题、{review_text}、{judge_n} 位评委怎么打分")
    for item in state.qa_history:
        score_text = f"{item['score']:.1f}" if item.get("score") is not None else "—"
        with st.expander(
            f"第 {item['index']} 题（主考官：{judge_label(item.get('writer', ''))}）"
            f"　本题最终分 {score_text} / 10",
            expanded=item["index"] == asked,
        ):
            st.markdown(f"**题目：** {item['question']}")
            if item.get("rewrites"):
                outcome = (
                    "重写版通过评审"
                    if item.get("passed")
                    else f"{item.get('rewrites', MAX_REWRITES)} 次重写仍未通过，已回退原题"
                )
                st.caption(
                    f"↩️ 主考官原题：{item.get('original', '')}（原题被否决，重写 {item['rewrites']} 次，{outcome}）"
                )
            st.markdown(f"**你的回答：** {item['answer']}")
            item_panel = review_size(item)
            item_judges = len(item.get("judge_results", []))
            st.markdown(
                f"**{item_panel} 位评审的投票结果**"
                if item_panel
                else "**出题评审：快速模式没有独立评审团（题目由唯一模型直接给出）**"
            )
            render_review_rounds(item.get("rounds", []))
            st.markdown(f"**{item_judges} 位评委的独立分数与评语**")
            st.markdown(judge_results_table(item.get("judge_results", [])))
            st.markdown(
                f"**本题最终分：{score_text} / 10**"
                f"　（{item_judges} 位评委的分数去掉一个最高分和一个最低分，剩下取平均）"
            )
            if item.get("reason"):
                st.caption(f"🚩 投「结束」的评委意见：{item['reason']}")

    # ---- 各评委总览（谁最严、谁最宽）----
    st.divider()
    matrix = judge_score_matrix(state)
    st.subheader(f"🧮 {len(matrix)} 位评委独立评分总览")
    st.markdown(
        markdown_table(
            ["评委", "各题独立分数", "平均分"],
            [
                [
                    row["label"],
                    "、".join(str(score) for score in row["scores"]) or "—",
                    row["average"] if row["average"] is not None else "—",
                ]
                for row in matrix
            ],
        )
    )
    rated = [row for row in matrix if row["average"] is not None]
    if len(rated) >= 2:
        strictest = min(rated, key=lambda row: row["average"] or 0)
        kindest = max(rated, key=lambda row: row["average"] or 0)
        st.caption(
            f"🔻 最严格：{strictest['label']}（平均 {strictest['average']}）　　"
            f"🔺 最宽容：{kindest['label']}（平均 {kindest['average']}）"
        )

    # ---- 反问环节 ----
    if state.reverse_history:
        st.divider()
        st.subheader("🙋 反问环节记录")
        render_reverse_history(state)

    # ---- AI 综合点评（改进建议直接不委婉）----
    st.divider()
    st.subheader("🧾 AI 综合点评（招聘委员会主审）")
    if state.report:
        st.markdown(state.report)
    else:
        st.warning(state.report_error or "AI 综合点评尚未生成。")
        if st.button("📝 重新生成 AI 点评", type="primary", use_container_width=True):
            with st.spinner("正在生成 AI 综合点评……"):
                generate_report()
            st.rerun()

    st.download_button(
        "⬇️ 下载报告（Markdown）",
        data=build_report_markdown(),
        file_name=f"模拟面试报告_{safe_filename(state.job)}.md",
        mime="text/markdown",
        use_container_width=True,
    )
    if st.button("🔄 再来一场面试", use_container_width=True):
        reset_session()
        st.rerun()


def format_history_score(value: Any) -> str:
    """把数据库里的分数格式化成一位小数；没有分数显示「—」。"""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "—"


def render_history() -> None:
    """右上角「历史记录」：读取本地 interview.db，按分数排序并展示进步趋势。"""
    st.subheader("📜 历史记录")
    try:
        records = load_interviews()
    except Exception as exc:  # noqa: BLE001 —— 本地数据库异常时不让整页崩掉
        st.warning(f"历史记录读取失败：{exc}")
        return
    if not records:
        st.info("还没有历史记录。完成一场面试并生成报告后，这里会自动出现记录。")
        return

    scored = [record for record in records if record.get("score") is not None]
    chronological = sorted(
        records,
        key=lambda record: (str(record.get("created_at") or ""), int(record.get("id") or 0)),
    )
    chronological_scored = [
        record for record in chronological if record.get("score") is not None
    ]
    best = max(scored, key=lambda record: record["score"]) if scored else None
    latest = chronological_scored[-1] if chronological_scored else None

    col1, col2, col3 = st.columns(3)
    col1.metric("总场次", f"{len(records)} 场")
    col2.metric("最高分", format_history_score(best["score"]) if best else "—")
    col3.metric("最近一场", format_history_score(latest["score"]) if latest else "—")

    if len(chronological_scored) >= 2:
        first = float(chronological_scored[0]["score"])
        last = float(chronological_scored[-1]["score"])
        previous = float(chronological_scored[-2]["score"])
        st.markdown("**📈 分数趋势（按时间排序）**")
        st.line_chart(
            {"最终平均分": [float(record["score"]) for record in chronological_scored]},
            height=220,
        )
        st.caption(
            f"第 1 场 {first:.1f} → 最近一场 {last:.1f}"
            f"（累计 {last - first:+.1f}，较上一场 {last - previous:+.1f}）"
            "　横轴：按时间排序的第 1 场 → 第 N 场"
        )
    elif len(chronological_scored) == 1:
        st.caption("再完成一场面试，就能看到分数趋势。")
    else:
        st.caption("当前记录没有有效分数，暂时无法生成趋势。")

    st.markdown("**全部记录（按分数从高到低）**")
    rows = [
        {
            "排名": rank,
            "岗位": record["job"],
            "公司": record["company"] or "—",
            "模式": record["mode"] or "—",
            "分数": format_history_score(record["score"]),
            "题数": record["questions_count"],
            "时间": record["created_at"],
        }
        for rank, record in enumerate(records, start=1)
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(f"💾 本地数据库：{DB_PATH}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title="面霸 AI", page_icon="🎯", layout="centered")
    init_session_state()
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)
    state = st.session_state

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 —— 数据库初始化失败时仍允许进行面试
        st.warning(f"本地历史记录数据库初始化失败：{exc}")

    _, history_col = st.columns([6.5, 1.1])
    with history_col:
        history_label = "✖️ 收起历史" if state.show_history else "📜 历史记录"
        if st.button(history_label, use_container_width=True, key="history_toggle"):
            state.show_history = not state.show_history
            st.rerun()
    if state.stage == "setup":
        render_hero()
    if state.show_history:
        render_history()
        st.divider()

    with st.sidebar:
        with st.expander("⚙️ 高级设置", expanded=False):
            st.markdown("**评委与模型**")
            st.markdown(
                "\n".join(
                    f"- {JUDGE_META[name]['label']}：`{current_model(name)}`"
                    + ("　（快模型）" if name in FAST_JUDGES else "")
                    for name in JUDGE_ORDER
                )
            )
            st.markdown(f"- 超时：每次调用 {REQUEST_TIMEOUT_SECONDS} 秒，超时跳过该评委")
            st.markdown(
                f"- 出题 / 重写上限：{QUESTION_MAX_TOKENS} token；"
                f"打分上限：{SCORE_MAX_TOKENS} token"
            )
            st.checkbox(QUICK_MODE_LABEL, key="quick_input", help=QUICK_MODE_HELP)
            if st.button("🔌 自检 6 位评委", use_container_width=True):
                with st.spinner("正在并发自检 6 位评委……"):
                    st.session_state.judge_health = run_judge_health_check()
                st.rerun()
            if state.judge_health:
                for name in JUDGE_ORDER:
                    info = state.judge_health.get(name) or {}
                    status = "✅" if info.get("ok") else "❌"
                    st.caption(
                        f"{status} {JUDGE_META[name]['label']} · "
                        f"{info.get('model', current_model(name))}"
                    )
            if state.stage == "setup" or not quick_mode():
                st.markdown(
                    f"- 出题：{len(FAST_JUDGES)} 个快模型出题 + {REVIEW_TEAM_SIZE} 位评审"
                )
                st.markdown(
                    f"- 打分：{len(JUDGE_ORDER)} 位评委全量并发（卡片展示四维度 + 评语），去极值取平均"
                )
            else:
                st.markdown(
                    f"- 快速模式：出题 / 打分只用 "
                    f"{JUDGE_META[QUICK_MODE_JUDGE]['label']} 一个模型"
                )
            if state.stage in {"interview", "report"}:
                st.divider()
                detail = (
                    f"**本场架构：** {architecture_label()}\n\n"
                    f"**当前岗位：** {state.job}\n\n"
                    f"**面试模式：** {state.mode}\n\n"
                    f"**公司规模：** {state.scale}"
                )
                if state.company:
                    detail += f"\n\n**目标企业：** {state.company}"
                st.markdown(detail)
                if st.button("🔄 重新开始", use_container_width=True):
                    reset_session()
                    st.rerun()

    if state.error:
        st.error(state.error)
        state.error = ""

    if state.stage == "setup":
        render_setup()
    elif state.stage == "confirm":
        render_suggest()
    elif state.stage == "interview":
        render_interview()
    else:
        render_report()


if __name__ == "__main__":
    main()




















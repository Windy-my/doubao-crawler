"""通用工具:日志、词表加载、账号加载、Markdown 与结果的持久化。

约定:
    * 每个词对应一个稳定的 ``task_id``(即词的 MD5),便于断点续爬与幂等。
    * 结构化结果以 **JSON Lines** 形式追加写入 ``results.json``,每行一个
      JSON 对象。相比维护 JSON 数组,追加写入在并发场景下更安全,也天然
      支持增量处理;读取时请逐行 ``json.loads``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

import config

logger = logging.getLogger("doubao.utils")

# 结果文件写入锁:本进程内的多个 Worker 协程共享同一把锁
_result_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def setup_logging(level: str | None = None) -> None:
    """初始化全局日志配置(幂等,可重复调用)。"""
    logging.basicConfig(
        level=getattr(logging, (level or config.LOG_LEVEL).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


# ---------------------------------------------------------------------------
# 标识符与文件名
# ---------------------------------------------------------------------------
def task_id_for(word: str) -> str:
    """由词生成稳定的任务 ID(MD5),用于断点续爬与结果关联。"""
    return hashlib.md5(word.strip().encode("utf-8")).hexdigest()


def safe_name(raw: str, suffix: str = "") -> str:
    """把任意字符串转为安全的文件名(取 MD5 十六进制),避免非法字符。

    :param raw: 原始字符串,如 task_id 或词。
    :param suffix: 追加的后缀,如 ``.md`` / ``.png``。
    """
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return f"{digest}{suffix}"


def repair_mojibake(text: str) -> str:
    """修复 "UTF-8 字节被 cp1252 误解码" 造成的乱码。

    豆包的 SSE 响应体会出现这种双重编码:真实中文的 UTF-8 字节先被当作
    cp1252 解读、再以 UTF-8 发送,于是"人工智能"变成 ``äººå·¥æ™ºèƒ½``。
    这里按字节反推还原。

    安全性:ASCII 在 cp1252 下是恒等映射,不受影响;若文本本身已是正常内容
    (含无法用 cp1252 表示的中文/emoji),则直接原样返回。
    """
    if not text:
        return text

    out = bytearray()
    for ch in text:
        code = ord(ch)
        if code < 0x80:
            out.append(code)
            continue
        try:
            out.extend(ch.encode("cp1252"))
        except UnicodeEncodeError:
            # cp1252 未定义的位点(0x81/0x8D/0x8F/0x90/0x9D)按 latin-1 取值
            if code <= 0xFF:
                out.append(code)
            else:
                return text  # 出现无法映射的字符,放弃修复
    try:
        return bytes(out).decode("utf-8")
    except UnicodeDecodeError:
        return text


# ---------------------------------------------------------------------------
# 词表与账号加载
# ---------------------------------------------------------------------------
def load_words(path: Path | None = None) -> list[str]:
    """加载词表,每行一个词。自动去除空行、首尾空白与重复项,保持原顺序。"""
    target = Path(path or config.WORDS_FILE)
    if not target.exists():
        logger.warning("词表文件不存在: %s", target)
        return []

    seen: set[str] = set()
    words: list[str] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        word = line.strip()
        # 跳过空行与以 # 开头的注释行
        if not word or word.startswith("#"):
            continue
        if word in seen:
            continue
        seen.add(word)
        words.append(word)
    logger.info("已加载词表: %d 个词 (%s)", len(words), target)
    return words


def load_accounts(path: Path | None = None) -> list[dict[str, Any]]:
    """加载账号配置。

    兼容两种结构::

        {"accounts": [ {...}, ... ]}
        [ {...}, ... ]

    每个账号至少包含 ``account_id`` 与 ``cookies``,``proxy`` 可选。
    """
    target = Path(path or config.ACCOUNTS_FILE)
    if not target.exists():
        logger.warning("账号文件不存在: %s", target)
        return []

    raw = json.loads(target.read_text(encoding="utf-8"))
    accounts = raw.get("accounts", []) if isinstance(raw, dict) else raw
    if not isinstance(accounts, list):
        raise ValueError(f"账号配置格式错误: {target}")

    result: list[dict[str, Any]] = []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("account_id") or "").strip()
        if not account_id:
            logger.warning("跳过缺少 account_id 的账号配置")
            continue
        cookies = item.get("cookies") or []
        if not cookies:
            logger.warning("账号 %s 未配置 cookies,已跳过", account_id)
            continue
        result.append(
            {
                "account_id": account_id,
                "cookies": cookies,
                "proxy": item.get("proxy") or None,
            }
        )
    logger.info("已加载账号: %d 个 (%s)", len(result), target)
    return result


# ---------------------------------------------------------------------------
# 结果持久化
# ---------------------------------------------------------------------------
async def append_result(record: dict[str, Any], path: Path | None = None) -> None:
    """把一条结构化结果以 JSON Lines 追加写入结果文件(带进程内锁)。"""
    target = Path(path or config.RESULT_FILE)
    line = json.dumps(record, ensure_ascii=False) + "\n"

    async with _result_lock:
        # 文件 IO 放到线程执行,避免阻塞事件循环
        await asyncio.to_thread(_append_line_sync, target, line)


def _append_line_sync(target: Path, line: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line)


async def write_text(path: Path, content: str) -> Path:
    """异步写入文本文件(自动创建父目录)。"""
    await asyncio.to_thread(_write_text_sync, path, content)
    return path


def _write_text_sync(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def save_markdown(task_id: str, markdown: str) -> Path:
    """保存正文 Markdown,文件名用 MD5 规避非法字符,返回保存路径。"""
    target = config.MARKDOWN_DIR / safe_name(task_id, ".md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown or "", encoding="utf-8")
    return target


def save_result_sync(record: dict[str, Any]) -> None:
    """同步版结果追加,供非异步上下文(如收尾阶段)调用。"""
    _append_line_sync(
        config.RESULT_FILE, json.dumps(record, ensure_ascii=False) + "\n"
    )


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    """把列表按固定大小切块,便于分批推送任务。"""
    for i in range(0, len(items), size):
        yield items[i : i + size]

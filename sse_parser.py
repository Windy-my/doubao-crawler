"""豆包 SSE 流解析器。

豆包的 ``/samantha/chat/completion`` 以 Server-Sent Events 返回,事件类型
有 7 种::

    SSE_HEARTBEAT    心跳,忽略
    SSE_ACK          建连确认,忽略
    FULL_MSG_NOTIFY  完整消息(非流式或流式收尾的完整快照)
    STREAM_MSG_NOTIFY 流式消息(累积快照)
    CHUNK_DELTA      增量分片
    STREAM_CHUNK     增量分片
    SSE_REPLY_END    回复结束

正文 Markdown 藏在 ``message.content_block`` 里(``content_block`` 可能是
被序列化成字符串的 JSON),需要按 ``block_type`` 提取::

    10000  文本块
    2074   图片块

参考链接藏在 ``message.reference_links`` 或 ``message.search_results`` 里,
每项通常含 ``url`` / ``title`` / ``snippet``。

**兼容策略**:由于豆包事件结构可能调整,本解析器不硬编码事件类型分支,而是
对每个事件的 ``data`` 统一走 ``_extract_from_data`` 做深度提取,并对文本采用
"快照替换 / 增量追加"的启发式去重,从而同时兼容累积快照与纯增量两种模式。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Iterable

import aiohttp

import config

logger = logging.getLogger("doubao.sse_parser")

# 豆包已知的 7 种 SSE 事件类型
EVENT_HEARTBEAT = "SSE_HEARTBEAT"
EVENT_ACK = "SSE_ACK"
EVENT_FULL_MSG = "FULL_MSG_NOTIFY"
EVENT_STREAM_MSG = "STREAM_MSG_NOTIFY"
EVENT_CHUNK_DELTA = "CHUNK_DELTA"
EVENT_STREAM_CHUNK = "STREAM_CHUNK"
EVENT_REPLY_END = "SSE_REPLY_END"
# 风控/限流会以该事件返回,例如 {"error_code":710022004,"error_msg":"rate limited"}
EVENT_STREAM_ERROR = "STREAM_ERROR"

# 需要忽略的事件(无正文内容)
IGNORED_EVENTS = {EVENT_HEARTBEAT, EVENT_ACK}

# content_block 的块类型
BLOCK_TYPE_TEXT = 10000
BLOCK_TYPE_IMAGE = 2074

# 参考链接里的 URL 字段候选名(兼容字段改名)
URL_KEYS = ("url", "link", "target_url", "source_url", "href")
TITLE_KEYS = ("title", "name", "site_name", "source")
SNIPPET_KEYS = ("snippet", "summary", "abstract", "desc", "description", "text")

# 图片 URL 字段候选名
IMAGE_URL_KEYS = ("url", "image_url", "image_uri", "uri", "src", "key")


class DoubaoSseParser:
    """解析豆包 SSE 流,产出正文 Markdown、参考链接与图片地址。

    既可流式解析(``parse_stream``),也可解析已经从页面内 fetch 拿到的
    完整 SSE 文本(``parse_text``)。
    """

    def __init__(self) -> None:
        self.reset()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空累积状态,便于复用同一个解析器实例。"""
        # CHUNK_DELTA 的纯增量片段(正文的主力来源)
        self._text_parts: list[str] = []
        # content_block 快照式文本(仅在完全没有增量时兜底)
        self._snapshot_parts: list[str] = []
        self._image_urls: list[str] = []
        # 错误事件(STREAM_ERROR),用于识别限流/风控
        self._errors: list[dict[str, Any]] = []
        self._reference_links: list[dict[str, str]] = []
        self._seen_ref_keys: set[str] = set()
        self._seen_image_urls: set[str] = set()
        self._event_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 流式解析(直连 aiohttp,一般用于调试;生产走页面内 fetch)
    # ------------------------------------------------------------------
    async def parse_stream(
        self,
        url: str,
        payload: dict[str, Any],
        cookies: Iterable[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """用 aiohttp 发起 POST 请求并解析 SSE 流。

        :param url: 完整请求地址。
        :param payload: 请求体(JSON)。
        :param cookies: cookie 字典列表,会拼成 ``Cookie`` 头。
        :param headers: 额外请求头。
        :return: ``_build_result()`` 的结果。
        """
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": config.REAL_UA,
            "Referer": config.DOUBAO_CHAT_URL,
        }
        if headers:
            request_headers.update(headers)

        cookie_header = self._build_cookie_header(cookies)
        if cookie_header:
            request_headers["Cookie"] = cookie_header

        timeout = aiohttp.ClientTimeout(total=config.SSE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, json=payload, headers=request_headers
            ) as response:
                response.raise_for_status()
                buffer = ""
                async for chunk in response.content.iter_any():
                    buffer += self._decode_chunk(chunk)
                    # 以空行切分事件;最后一段可能不完整,留到下一轮
                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        self._handle_event(event_str)
                if buffer.strip():
                    self._handle_event(buffer)

        return self._build_result()

    async def parse_stream_iter(
        self, url: str, payload: dict[str, Any], cookies: Iterable[dict[str, Any]] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """逐事件产出解析中间态,便于实时观察(可选能力)。"""
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": config.REAL_UA,
            "Referer": config.DOUBAO_CHAT_URL,
        }
        cookie_header = self._build_cookie_header(cookies)
        if cookie_header:
            request_headers["Cookie"] = cookie_header

        timeout = aiohttp.ClientTimeout(total=config.SSE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, json=payload, headers=request_headers
            ) as response:
                response.raise_for_status()
                buffer = ""
                async for chunk in response.content.iter_any():
                    buffer += self._decode_chunk(chunk)
                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        self._handle_event(event_str)
                        yield self._build_result()

    # ------------------------------------------------------------------
    # 文本解析(Playwright 页面内 fetch 拿到的完整 SSE 文本)
    # ------------------------------------------------------------------
    def parse_text(self, raw_text: str) -> dict[str, Any]:
        """解析一份完整的 SSE 响应文本。

        这是生产环境的主路径:由 ``page.evaluate`` 在浏览器上下文内 ``fetch``
        拿到响应后,把文本交给本方法解析。
        """
        if not raw_text:
            return self._build_result()

        for event_str in raw_text.replace("\r\n", "\n").split("\n\n"):
            if event_str.strip():
                self._handle_event(event_str)
        return self._build_result()

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def _handle_event(self, event_str: str) -> None:
        """解析单个 SSE 事件块(可能含 ``event:`` 与多行 ``data:``)。"""
        event_name = ""
        data_parts: list[str] = []

        for line in event_str.split("\n"):
            line = line.rstrip("\r")
            if not line or line.startswith(":"):
                # 空行或注释行
                continue
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_parts.append(line[len("data:") :].strip())

        if not data_parts:
            return

        data_str = "\n".join(data_parts)
        # SSE 流结束标记
        if data_str in ("[DONE]", "done"):
            return

        self._event_counts[event_name or "UNKNOWN"] = (
            self._event_counts.get(event_name or "UNKNOWN", 0) + 1
        )

        if event_name in IGNORED_EVENTS:
            return

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            logger.debug("跳过非 JSON 事件: %s", data_str[:200])
            return

        # 豆包风控/限流以 STREAM_ERROR 事件返回,必须单独记录而不是当正文处理
        if event_name == EVENT_STREAM_ERROR:
            self._errors.append(data if isinstance(data, dict) else {"raw": data})
            message = data.get("error_msg") if isinstance(data, dict) else ""
            logger.warning("豆包返回错误事件: %s", message or data)
            return

        self._extract_from_data(data)

    # ------------------------------------------------------------------
    # 数据提取
    # ------------------------------------------------------------------
    def _extract_from_data(self, data: Any) -> None:
        """从事件 data 中提取文本、图片与参考链接。

        需要兼容三种承载正文的事件形态(实测):

        * ``CHUNK_DELTA``  —— 顶层直接是 ``{"text": "增量片段"}``
        * ``STREAM_CHUNK`` —— ``patch_op[].patch_value.content_block[]``
        * ``FULL_MSG_NOTIFY`` / ``STREAM_MSG_NOTIFY`` —— ``message.content_block[]``
        """
        # 形态一:顶层直接给 text(CHUNK_DELTA,AI 回答的主力来源)
        if isinstance(data, dict):
            direct_text = data.get("text")
            if isinstance(direct_text, str) and direct_text:
                # CHUNK_DELTA 是纯增量,必须直接拼接,不能走去重启发式
                self._add_text(direct_text, is_delta=True)

        # 形态二/三:通过 message / patch_value 中的 content_block 提取
        for message in self._iter_messages(data):
            self._extract_blocks(message)
            self._extract_references(message)

    def _iter_messages(self, node: Any) -> Iterable[dict[str, Any]]:
        """深度遍历,产出所有疑似 message 的字典节点。"""
        if isinstance(node, dict):
            if (
                "content_block" in node
                or "search_query_result_block" in node
                or "reference_links" in node
                or "search_results" in node
            ):
                yield node
            for value in node.values():
                yield from self._iter_messages(value)
        elif isinstance(node, list):
            for item in node:
                yield from self._iter_messages(item)
        elif isinstance(node, str):
            # message.content 可能是被序列化的 JSON 字符串
            stripped = node.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    yield from self._iter_messages(json.loads(stripped))
                except json.JSONDecodeError:
                    return

    def _iter_content_blocks(self, message: dict[str, Any]) -> Iterable[dict[str, Any]]:
        """从 message 中逐个产出 content_block。"""
        container = message.get("content_block")
        if container is None:
            content = message.get("content")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content = None
            if isinstance(content, dict):
                container = content.get("content_block")
            elif isinstance(content, list):
                container = content

        if isinstance(container, dict):
            container = [container]
        if not isinstance(container, list):
            return

        for block in container:
            if not isinstance(block, dict):
                continue
            # 有些结构会把块再包一层,统一展开
            if "content_block" in block and "block_type" not in block:
                nested = block.get("content_block")
                if isinstance(nested, dict):
                    yield nested
                    continue
                if isinstance(nested, list):
                    for item in nested:
                        if isinstance(item, dict):
                            yield item
                    continue
            yield block

    def _extract_blocks(self, message: dict[str, Any]) -> None:
        """提取文本块与图片块。

        注意:用户自己发送的消息(``user_type == 1``)不纳入正文,否则最终
        Markdown 会把提问和回答粘在一起。
        """
        is_user_message = self._coerce_int(message.get("user_type")) == 1
        for block in self._iter_content_blocks(message):
            block_type = self._coerce_int(
                self._first_key(block, ("block_type", "type", "content_type"))
            )

            if block_type == BLOCK_TYPE_TEXT:
                if is_user_message:
                    continue
                text = self._extract_block_text(block)
                if text:
                    self._add_text(text)

            elif block_type == BLOCK_TYPE_IMAGE:
                url = self._first_key(block, IMAGE_URL_KEYS)
                if not url:
                    # 图片信息可能嵌套在 image / image_info 字段里
                    nested = block.get("image") or block.get("image_info")
                    if isinstance(nested, dict):
                        url = self._first_key(nested, IMAGE_URL_KEYS)
                if isinstance(url, str) and url:
                    self._add_image(url)

    def _extract_block_text(self, block: dict[str, Any]) -> str | None:
        """从文本块中提取正文。

        实测豆包的结构为::

            {"block_type": 10000, "content": {"text_block": {"text": "..."}}}

        同时兼容 ``content`` 直接是字符串、或 ``text`` 位于块顶层的旧结构。
        """
        content = block.get("content")

        # 1) 真实结构:content.text_block.text
        if isinstance(content, dict):
            text_block = content.get("text_block")
            if isinstance(text_block, dict):
                text = text_block.get("text")
                if isinstance(text, str) and text:
                    return text
            # 其它可能的键名
            for key in ("text", "content", "markdown"):
                value = content.get(key)
                if isinstance(value, str) and value:
                    return value

        # 2) content 直接是字符串
        if isinstance(content, str) and content:
            return content

        # 3) 块顶层直接给 text
        for key in ("text", "markdown"):
            value = block.get(key)
            if isinstance(value, str) and value:
                return value

        return None

    def _extract_references(self, message: dict[str, Any]) -> None:
        """提取参考链接。

        主来源是联网检索结果块(实测)::

            {"search_query_result_block": {"results": [
                {"text_card": {"title": "...", "url": "..."}}]}}

        同时兼容 ``reference_links`` / ``search_results`` 等字段。
        """
        # 1) 联网检索结果 —— 豆包返回参考链接的实际位置
        search_block = message.get("search_query_result_block")
        if isinstance(search_block, dict):
            for item in search_block.get("results") or []:
                if not isinstance(item, dict):
                    continue
                # 卡片字段名可能变化:text_card / web_card / card / 直接平铺
                card = (
                    item.get("text_card")
                    or item.get("web_card")
                    or item.get("card")
                    or item
                )
                if not isinstance(card, dict):
                    continue
                url = self._first_key(card, URL_KEYS)
                if not isinstance(url, str) or not url:
                    continue
                title = self._first_key(card, TITLE_KEYS) or ""
                snippet = self._first_key(card, SNIPPET_KEYS) or ""
                self._add_reference(
                    url=url,
                    title=title if isinstance(title, str) else str(title),
                    snippet=snippet if isinstance(snippet, str) else str(snippet),
                )

        # 2) 其它可能的字段
        for field in ("reference_links", "search_results", "references", "docs"):
            entries = message.get(field)
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                url = self._first_key(entry, URL_KEYS)
                if not isinstance(url, str) or not url:
                    continue
                title = self._first_key(entry, TITLE_KEYS) or ""
                snippet = self._first_key(entry, SNIPPET_KEYS) or ""
                self._add_reference(
                    url=url,
                    title=title if isinstance(title, str) else str(title),
                    snippet=snippet if isinstance(snippet, str) else str(snippet),
                )

    # ------------------------------------------------------------------
    # 去重与累积
    # ------------------------------------------------------------------
    def _add_text(self, text: str, is_delta: bool = False) -> None:
        """把文本片段合入正文。

        :param is_delta: True 表示这是 ``CHUNK_DELTA`` 的**纯增量**,按顺序直接
            拼接——绝不能走去重启发式,否则"的"、"是"这类短片段会被误删;
            False 表示可能是累积快照,按包含关系去重。
        """
        if not text:
            return

        if is_delta:
            self._text_parts.append(text)
            return

        current = "".join(self._snapshot_parts)
        if not current:
            self._snapshot_parts = [text]
            return
        if text == current:
            # 完全重复的快照
            return
        # 较长文本才做"包含关系"判定,避免吞掉 "\n\n" 这类合法短增量
        if len(text) >= 8 and (text.startswith(current) or current in text):
            # 新片段包含已有全文 → 更完整的快照,直接替换
            self._snapshot_parts = [text]
            return
        if len(current) >= 8 and (current.startswith(text) or text in current):
            # 收到的是更短/更旧的快照 → 忽略
            return
        if current.endswith(text):
            # 快照内容已包含在末尾,忽略
            return
        self._snapshot_parts.append(text)

    def _add_image(self, url: str) -> None:
        """按 URL 去重收集图片。"""
        if url in self._seen_image_urls:
            return
        self._seen_image_urls.add(url)
        self._image_urls.append(url)

    def _add_reference(self, url: str, title: str, snippet: str) -> None:
        """按 URL 去重收集参考链接,保持出现顺序。"""
        key = url
        if key in self._seen_ref_keys:
            return
        self._seen_ref_keys.add(key)
        self._reference_links.append(
            {"url": url, "title": title, "snippet": snippet}
        )

    # ------------------------------------------------------------------
    # 结果
    # ------------------------------------------------------------------
    def _build_result(self) -> dict[str, Any]:
        """组装最终结果。

        正文优先采用 CHUNK_DELTA 的增量拼接结果;仅当完全没有增量时才回退到
        content_block 快照,避免两种来源重复堆叠。
        """
        markdown = "".join(self._text_parts).strip()
        if not markdown:
            markdown = "".join(self._snapshot_parts).strip()
        return {
            "markdown": markdown,
            "reference_links": list(self._reference_links),
            "image_urls": list(self._image_urls),
            "has_reference": bool(self._reference_links),
            "event_counts": dict(self._event_counts),
            "errors": list(self._errors),
            "rate_limited": self.is_rate_limited(),
        }

    def is_rate_limited(self) -> bool:
        """本次响应是否被豆包限流/风控拦截。"""
        for err in self._errors:
            message = str(err.get("error_msg") or "").lower()
            if "rate limit" in message or "rate_limit" in message:
                return True
        return False

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _first_key(obj: dict[str, Any], keys: Iterable[str]) -> Any:
        """返回 obj 中第一个存在且非空的候选键值。"""
        for key in keys:
            if key in obj:
                value = obj[key]
                if value not in (None, "", [], {}):
                    return value
        return None

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        """尽量把块类型转成整数。"""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _decode_chunk(chunk: bytes) -> str:
        """按 UTF-8 解码分片,容忍跨分片截断的多字节字符。"""
        return chunk.decode("utf-8", errors="replace")

    @staticmethod
    def _build_cookie_header(cookies: Iterable[dict[str, Any]] | None) -> str:
        """把 cookie 字典列表拼成 ``Cookie`` 请求头。"""
        if not cookies:
            return ""
        return "; ".join(
            f"{c.get('name')}={c.get('value')}"
            for c in cookies
            if c.get("name") and c.get("value") is not None
        )

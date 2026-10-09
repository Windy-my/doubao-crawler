"""截图工具。

截图时机非常关键:必须在 SSE 流完全结束、页面渲染完成后进行,并且要先滚动
到底部,确保参考链接区域进入可视范围,否则 ``full_page`` 也可能截不到未渲染
的内容。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import config
import utils

logger = logging.getLogger("doubao.screenshot")

# 等待 AI 回复渲染完成的候选选择器(豆包前端可能调整,做多路兼容)
MESSAGE_SELECTORS = (
    "[data-testid='message-content']",
    "[data-testid='message_text_content']",
    "[data-testid='receive_message']",
    ".message-content",
)

# 滚动到底部的 JS:多候选滚动容器,兜底用 window
_SCROLL_TO_BOTTOM_JS = """
() => {
    const candidates = [
        document.scrollingElement,
        document.documentElement,
        document.body,
        ...document.querySelectorAll('[class*="scroll"], [class*="chat"], main'),
    ].filter(Boolean);
    for (const el of candidates) {
        try {
            el.scrollTop = el.scrollHeight;
        } catch (e) { /* 忽略不可滚动元素 */ }
    }
    window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
}
"""


async def wait_for_render(page, timeout: int | None = None) -> bool:
    """等待 AI 回复渲染完成。

    依次尝试多个候选选择器,任一命中即认为渲染完成;全部超时则返回 False
    (不抛异常,交给上层决定是否仍然截图)。
    """
    limit = timeout or config.RENDER_TIMEOUT
    per_selector = max(int(limit / len(MESSAGE_SELECTORS)), 3000)

    for selector in MESSAGE_SELECTORS:
        try:
            await page.wait_for_selector(selector, timeout=per_selector, state="visible")
            logger.debug("回复渲染完成,命中选择器: %s", selector)
            return True
        except Exception:  # noqa: BLE001 - Playwright 超时异常类型较多,统一兜底
            continue

    logger.warning("等待回复渲染超时(尝试了 %d 个选择器)", len(MESSAGE_SELECTORS))
    return False


async def scroll_to_bottom(page) -> None:
    """滚动到页面底部,确保参考链接区域可见。"""
    try:
        await page.evaluate(_SCROLL_TO_BOTTOM_JS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("滚动到底部失败(忽略): %s", exc)


async def screenshot_conversation(
    page, task_id: str, wait_timeout: int | None = None
) -> Path | None:
    """等待渲染 → 滚动到底 → 全页截图。

    :param page: Playwright Page 对象。
    :param task_id: 任务 ID,用于生成安全的文件名(MD5)。
    :param wait_timeout: 渲染检测的等待毫秒数;None 表示使用默认值。
    :return: 截图文件路径;失败返回 None。
    """
    filepath = config.SCREENSHOT_DIR / utils.safe_name(task_id, ".png")
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # 1) 等待 AI 回复渲染完成
    await wait_for_render(page, timeout=wait_timeout)

    # 2) 稳定 1 秒后滚动到底部,再等 0.5 秒让懒加载内容就位
    await asyncio.sleep(1.0)
    await scroll_to_bottom(page)
    await asyncio.sleep(0.5)

    # 3) 全页截图
    try:
        await page.screenshot(
            path=str(filepath),
            full_page=True,
            timeout=config.SCREENSHOT_TIMEOUT,
        )
        logger.info("截图已保存: %s", filepath)
        return filepath
    except Exception as exc:  # noqa: BLE001
        logger.error("截图失败 (%s): %s", task_id, exc)
        return None


async def screenshot_element(page, selector: str, task_id: str) -> Path | None:
    """只截取某个元素(可选能力,用于精确截取回复区域)。"""
    filepath = config.SCREENSHOT_DIR / utils.safe_name(f"{task_id}:{selector}", ".png")
    try:
        element = await page.wait_for_selector(selector, timeout=config.RENDER_TIMEOUT)
        if element is None:
            return None
        await element.screenshot(path=str(filepath), timeout=config.SCREENSHOT_TIMEOUT)
        return filepath
    except Exception as exc:  # noqa: BLE001
        logger.error("元素截图失败 (%s): %s", task_id, exc)
        return None

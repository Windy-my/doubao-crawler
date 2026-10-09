"""配置模块:集中管理豆包爬虫的运行参数、路径、代理与 Redis 连接信息。

所有可调参数集中在此处,便于在不同环境下快速调整。凡涉及风控相关的
阈值(健康分、冷却时长、连续无参考链接次数)都带有注释说明其含义。

注意:所有目录常量在导入时即会被创建,避免运行期出现 FileNotFoundError。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 基础路径
# ---------------------------------------------------------------------------
# 项目根目录(本文件所在目录)
BASE_DIR: Path = Path(__file__).resolve().parent
# 数据目录:词表、账号、正文、截图、结果的统一存放位置
DATA_DIR: Path = Path(os.getenv("DOUBAO_DATA_DIR", BASE_DIR / "data"))

# 待查询词表:每行一个词
WORDS_FILE: Path = Path(os.getenv("DOUBAO_WORDS_FILE", DATA_DIR / "words.txt"))
# 账号配置:cookies + 绑定代理
ACCOUNTS_FILE: Path = Path(os.getenv("DOUBAO_ACCOUNTS_FILE", DATA_DIR / "accounts.json"))

# 正文 Markdown 保存目录
MARKDOWN_DIR: Path = Path(os.getenv("DOUBAO_MARKDOWN_DIR", DATA_DIR / "markdown"))
# 截图保存目录
SCREENSHOT_DIR: Path = Path(os.getenv("DOUBAO_SCREENSHOT_DIR", DATA_DIR / "screenshots"))
# 结构化结果文件(JSON Lines 之外的汇总文件)
RESULT_FILE: Path = Path(os.getenv("DOUBAO_RESULT_FILE", DATA_DIR / "results.json"))

# 导入时确保目录存在,省去各模块自行创建
for _directory in (DATA_DIR, MARKDOWN_DIR, SCREENSHOT_DIR):
    _directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Redis 连接
# ---------------------------------------------------------------------------
REDIS_HOST: str = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB: int = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD: str | None = os.getenv("REDIS_PASSWORD") or None

# ---------------------------------------------------------------------------
# 并发与浏览器
# ---------------------------------------------------------------------------
# 实测:豆包对同一账号的密集请求会返回 rate limited(error_code 710022004)。
# 默认只开 1 个并发 Page,并在任务之间留出间隔(见 TASK_INTERVAL)——这是最能
# 稳定拿到参考链接的配置。账号多时可以调高,但要注意总量仍受平台限流约束。
MAX_CONCURRENT_PAGES: int = int(os.getenv("MAX_CONCURRENT_PAGES", "1"))

# 真实 Chrome UA:使用随机化过时的 UA 反而更容易被识别为机器人
REAL_UA: str = os.getenv(
    "DOUBAO_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

# 浏览器视口:与常见桌面分辨率保持一致
VIEWPORT: dict[str, int] = {"width": 1440, "height": 900}

# 是否以无头模式运行;如需人工排查风控可设为 False
HEADLESS: bool = os.getenv("DOUBAO_HEADLESS", "1") not in ("0", "false", "False")

# 页面内 fetch 不会驱动豆包前端状态机,截图前若检测到回复未渲染,
# 是否把解析结果注入 DOM 以保证截图内容可见(详见 worker.py 顶部说明)。
INJECT_RENDER_FALLBACK: bool = os.getenv("DOUBAO_INJECT_RENDER", "1") not in (
    "0",
    "false",
    "False",
)

# Chromium 启动参数:关闭自动化特征,降低被识别概率
BROWSER_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
    "--lang=zh-CN",
]

# ---------------------------------------------------------------------------
# 交互模式与浏览器 profile
# ---------------------------------------------------------------------------
# 豆包 completion 接口的 URL 带有 a_bogus / msToken / device_id 等由页面 JS
# 动态生成的签名参数,页面内裸 fetch 无法构造。因此默认走 "ui" 模式:
# 驱动真实页面输入并发送,再捕获页面自己发出的响应。
#   "ui"    —— 推荐,签名由豆包前端计算
#   "fetch" —— 页面内直接 fetch(仅当签名不校验时可用,保留作对照)
DOUBAO_INTERACTION_MODE: str = os.getenv("DOUBAO_INTERACTION_MODE", "ui")

# 是否复用持久化 profile(已登录的浏览器数据目录),而不是临时新建上下文
USE_PERSISTENT_PROFILE: bool = os.getenv("DOUBAO_PERSISTENT", "0") not in (
    "0",
    "false",
    "False",
)
# 持久化 profile 目录(存放登录态)
DOUBAO_PROFILE_DIR: str = os.getenv(
    "DOUBAO_PROFILE_DIR", str(BASE_DIR / ".doubao_profile")
)
# 浏览器 channel:留空使用 Playwright 自带 Chromium(可跨会话保留 cookie);
# 设为 "msedge" 会使用 Edge,但其 App-Bound Encryption 会导致 cookie 无法复用。
BROWSER_CHANNEL: str = os.getenv("DOUBAO_BROWSER_CHANNEL", "")
# UI 模式下等待 completion 响应的最长秒数
UI_RESPONSE_TIMEOUT: int = int(os.getenv("UI_RESPONSE_TIMEOUT", "220"))
# 是否保存原始 SSE 响应文本(调试用,存到 data/raw_sse/)
SAVE_RAW_SSE: bool = os.getenv("DOUBAO_SAVE_RAW_SSE", "0") not in (
    "0",
    "false",
    "False",
)
# UI 模式下是否自动点击"联网搜索"开关。豆包的开关选择器易变,且误点会破坏页面
# 状态,故默认关闭;需要抓参考链接时再按实际 DOM 校准后开启。
ENABLE_WEB_SEARCH_TOGGLE: bool = os.getenv("DOUBAO_ENABLE_WEB_SEARCH", "0") not in (
    "0",
    "false",
    "False",
)

# ---------------------------------------------------------------------------
# 豆包接口
# ---------------------------------------------------------------------------
DOUBAO_CHAT_URL: str = "https://www.doubao.com/chat/"
# 实际抓包确认的接口路径(规格里写的 /samantha/chat/completion 已不再使用)
DOUBAO_COMPLETION_API: str = "/chat/completion"

# 页面内 fetch 的请求路径前缀(浏览器上下文中的绝对路径)
DOUBAO_ORIGIN: str = "https://www.doubao.com"

# ---------------------------------------------------------------------------
# 账号健康分与冷却策略
# ---------------------------------------------------------------------------
# 低于该健康分不再分配给任务
ACCOUNT_MIN_HEALTH: int = int(os.getenv("ACCOUNT_MIN_HEALTH", "60"))
# 健康分低于该阈值 → 直接冷却
LOW_HEALTH_THRESHOLD: int = int(os.getenv("LOW_HEALTH_THRESHOLD", "40"))
# 健康分判定所需的最小样本数:避免单次偶发失败(网络抖动/超时)就把账号打入长冷却
MIN_SAMPLES_FOR_COOLING: int = int(os.getenv("MIN_SAMPLES_FOR_COOLING", "3"))
# 连续无参考链接次数上限,达到即冷却
NO_REF_STREAK_LIMIT: int = int(os.getenv("NO_REF_STREAK_LIMIT", "3"))
# 常规冷却时长(秒):连续无参考链接触发
COOLING_TIME: int = int(os.getenv("COOLING_TIME", "3600"))
# 深度冷却时长(秒):健康分过低触发
HEALTH_COOLING_TIME: int = int(os.getenv("HEALTH_COOLING_TIME", "7200"))
# 被豆包限流(rate limited)时的冷却时长(秒),通常需要等更久
RATE_LIMIT_COOLING_TIME: int = int(os.getenv("RATE_LIMIT_COOLING_TIME", "1800"))
# 检测到限流时是否立即结束整个测试(用于试探"最小安全请求间隔":
# 一旦触发限流就停下,当时的间隔即为临界值),默认关闭
STOP_ON_RATE_LIMIT: bool = os.getenv("DOUBAO_STOP_ON_RATE_LIMIT", "0") not in (
    "0",
    "false",
    "False",
)
# 冷却结束重新激活时的健康分(给予一次"复活"机会)
REACTIVATE_HEALTH: int = int(os.getenv("REACTIVATE_HEALTH", "60"))
# 同一账号两次查询的最小间隔(秒)。
# 实测这个间隔是避免 rate limited 的关键,故默认取 90 秒。
MIN_ACCOUNT_INTERVAL: int = int(os.getenv("MIN_ACCOUNT_INTERVAL", "90"))

# 健康分权重:成功率 40%,参考链接率 60%
SUCCESS_WEIGHT: float = 0.4
REFERENCE_WEIGHT: float = 0.6

# 降权巡检:总查询数达到该值后开始评估参考链接返回率
DEGRADATION_MIN_QUERIES: int = int(os.getenv("DEGRADATION_MIN_QUERIES", "10"))
# 参考链接返回率低于该比例 → 判定为已降权
DEGRADATION_REF_RATE: float = float(os.getenv("DEGRADATION_REF_RATE", "0.3"))

# ---------------------------------------------------------------------------
# 超时与重试
# ---------------------------------------------------------------------------
# 单个任务整体超时(秒)
TASK_TIMEOUT: int = int(os.getenv("TASK_TIMEOUT", "280"))
# 每处理完一个任务后的固定间隔(秒)。豆包对密集请求会限流,间隔是最有效的规避手段;
# 设为 0 表示不等待(仅建议在多账号 + 代理轮换时使用)。
TASK_INTERVAL: int = int(os.getenv("TASK_INTERVAL", "90"))
# SSE 流读取超时(秒)
SSE_TIMEOUT: int = int(os.getenv("SSE_TIMEOUT", "90"))
# 页面导航超时(毫秒)
PAGE_GOTO_TIMEOUT: int = int(os.getenv("PAGE_GOTO_TIMEOUT", "60000"))
# 等待 AI 回复渲染超时(毫秒)
RENDER_TIMEOUT: int = int(os.getenv("RENDER_TIMEOUT", "30000"))
# 截图超时(毫秒)
SCREENSHOT_TIMEOUT: int = int(os.getenv("SCREENSHOT_TIMEOUT", "15000"))
# 任务最大重试次数,超过进入死信队列
MAX_RETRY: int = int(os.getenv("MAX_RETRY", "3"))
# queue:running 中任务超过该秒数视为超时,由 recover() 重新入队
RUNNING_TIMEOUT: int = int(os.getenv("RUNNING_TIMEOUT", "180"))
# 无可用账号时的等待秒数
NO_ACCOUNT_WAIT: int = int(os.getenv("NO_ACCOUNT_WAIT", "10"))
# 连续多少轮拿不到账号就退出该 Worker(避免账号全部冷却时无限挂起)
MAX_NO_ACCOUNT_ROUNDS: int = int(os.getenv("MAX_NO_ACCOUNT_ROUNDS", "18"))

# ---------------------------------------------------------------------------
# Redis 键名
# ---------------------------------------------------------------------------
QUEUE_PENDING: str = "queue:pending"
QUEUE_RUNNING: str = "queue:running"
QUEUE_DONE: str = "queue:done"
QUEUE_DEAD_LETTER: str = "queue:dead_letter"

ACCOUNT_KEY_PREFIX: str = "account:"
POOL_ACTIVE: str = "pool:active"
POOL_COOLING: str = "pool:cooling"

# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------
# 结果 JSON 写入锁的键,避免多 Worker 并发写坏文件
RESULT_LOCK_KEY: str = "lock:result_file"
# 日志级别
LOG_LEVEL: str = os.getenv("DOUBAO_LOG_LEVEL", "INFO")


# ---------------------------------------------------------------------------
# CDP 模式:连接已运行的 Chrome 实例
# ---------------------------------------------------------------------------
# 为什么改 CDP:
#   Playwright 自己 launch 出来的 Chromium 会带上自动化特征(navigator.webdriver、
#   CDP Runtime.enable 泄露、无头指纹、缺少扩展/WebGL/Canvas 等),豆包风控能直接
#   识别并限流 —— 表现为"同一账号在 Edge 里正常,一换成 Playwright 就被限流"。
#   改为连接一个**真实启动的 Chrome**,指纹与真人浏览器一致。
#
# 浏览器可执行文件;留空则自动探测 Chrome → Edge
CHROME_PATH: str = os.getenv("DOUBAO_CHROME_PATH", "")

# 各账号 CDP 调试端口的起始值(按账号顺序递增,9222/9223/...)
CDP_PORT_BASE: int = int(os.getenv("CDP_PORT_BASE", "9222"))

# 连接 CDP 与执行验证门的超时(秒)
CDP_CONNECT_TIMEOUT: int = int(os.getenv("CDP_CONNECT_TIMEOUT", "20"))

# 采集前若实例未运行,是否自动拉起 Chrome
AUTO_LAUNCH_CHROME: bool = os.getenv("DOUBAO_AUTO_LAUNCH", "1") not in (
    "0",
    "false",
    "False",
)

# 采集结束后是否关闭 Chrome 实例(默认保留,便于下次直接连接、登录态不丢)
CLOSE_CHROME_ON_EXIT: bool = os.getenv("DOUBAO_CLOSE_CHROME", "0") not in (
    "0",
    "false",
    "False",
)

# 是否优先使用 patchright(已安装时)。patchright 修复了 Playwright 在 CDP 上的
# Runtime.enable 泄露;未安装则自动回退到 playwright。
PREFER_PATCHRIGHT: bool = os.getenv("DOUBAO_PREFER_PATCHRIGHT", "1") not in (
    "0",
    "false",
    "False",
)

# 代理绕过列表(传给 Chrome 的 --proxy-bypass-list)
CHROME_PROXY_BYPASS: str = os.getenv("DOUBAO_PROXY_BYPASS", "<-loopback>")

# ---------------------------------------------------------------------------
# 拟人化节奏(降低被判定为自动化的概率)
# ---------------------------------------------------------------------------
# 任务之间的随机间隔区间(秒)。固定间隔本身就是自动化特征,故改为随机。
# 本次测试要求:每账号间隔 100-150 秒随机(均 > 30 秒,避免触发 IP/机器码限流)。
TASK_INTERVAL_MIN: int = int(os.getenv("TASK_INTERVAL_MIN", "100"))
TASK_INTERVAL_MAX: int = int(os.getenv("TASK_INTERVAL_MAX", "150"))
# 同一账号两次查询的最小随机间隔(秒)
ACCOUNT_INTERVAL_MIN: int = int(os.getenv("ACCOUNT_INTERVAL_MIN", "100"))
ACCOUNT_INTERVAL_MAX: int = int(os.getenv("ACCOUNT_INTERVAL_MAX", "150"))
# 页面动作之间的随机等待区间(秒)
ACTION_DELAY_MIN: float = float(os.getenv("ACTION_DELAY_MIN", "0.5"))
ACTION_DELAY_MAX: float = float(os.getenv("ACTION_DELAY_MAX", "1.8"))
# 冷却恢复后需要"温柔"处理的请求次数(期间使用加倍间隔)
RECOVERY_REQUESTS: int = int(os.getenv("RECOVERY_REQUESTS", "5"))
# 恢复期的间隔倍数
RECOVERY_INTERVAL_FACTOR: float = float(
    os.getenv("RECOVERY_INTERVAL_FACTOR", "2.0")
)


def random_task_interval() -> float:
    """返回一次随机的任务间隔(秒)。"""
    import random

    return random.uniform(TASK_INTERVAL_MIN, TASK_INTERVAL_MAX)


def random_account_interval() -> float:
    """返回一次随机的同账号最小间隔(秒)。"""
    import random

    return random.uniform(ACCOUNT_INTERVAL_MIN, ACCOUNT_INTERVAL_MAX)


def random_action_delay() -> float:
    """返回一次随机的动作间隔(秒),用于模拟真人操作节奏。"""
    import random

    return random.uniform(ACTION_DELAY_MIN, ACTION_DELAY_MAX)


# ---------------------------------------------------------------------------
# 间隔阶梯(用于试探"单个账号的最小安全请求间隔")
# ---------------------------------------------------------------------------
# 格式:逗号分隔的秒数,例如 "60,45,30,20,12,6,3"。
# 每完成 TASK_INTERVAL_LADDER_STEP 个任务降一档;为空则使用上面的随机区间。
TASK_INTERVAL_LADDER: str = os.getenv("DOUBAO_INTERVAL_LADDER", "")
TASK_INTERVAL_LADDER_STEP: int = int(os.getenv("DOUBAO_INTERVAL_STEP", "7"))


def ladder_intervals() -> list[float]:
    """解析阶梯配置,返回秒数列表(未配置则返回空列表)。"""
    raw = TASK_INTERVAL_LADDER.strip()
    if not raw:
        return []
    result: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(float(part))
        except ValueError:
            continue
    return result


def ladder_interval(finished_count: int) -> float | None:
    """按"已完成任务数"返回当前阶梯应使用的间隔(秒)。

    未配置阶梯时返回 None,由调用方回退到随机区间。
    """
    ladder = ladder_intervals()
    if not ladder:
        return None
    step = max(1, TASK_INTERVAL_LADDER_STEP)
    index = min(finished_count // step, len(ladder) - 1)
    return ladder[index]

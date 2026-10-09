# 账号池
交互版
.venv\Scripts\python.exe scripts\add_account.py
:: 在项目根目录执行(用虚拟环境里的 python,系统 PATH 里的 python 是 Store 占位符)
.venv\Scripts\python.exe scripts\add_account.py --id account_2

:: 强烈建议绑定固定代理(频繁换 IP 会直接触发降权)
.venv\Scripts\python.exe scripts\add_account.py --id account_2 --proxy http://127.0.0.1:18080

:: 带用户名密码的代理
.venv\Scripts\python.exe scripts\add_account.py --id account_2 --proxy http://user:pass@1.2.3.4:8080

:: 查看当前所有账号
.venv\Scripts\python.exe scripts\add_account.py --list

# 确认换 IP 是否生效
.venv\Scripts\python.exe scripts\check_ip.py

# doubao-crawler

在单台 8 核 16G 机器上批量采集**豆包网页版**的回答:解析 SSE 流中的正文 Markdown 与参考链接,并对会话截图保存。

## 架构:CDP 模式(连接真实 Chrome)

**不再由 Playwright 启动浏览器**,而是让每个账号跑在**一个独立启动的真实 Chrome 实例**里
(独立 User Data Dir + 独立调试端口),Playwright 只通过 `connect_over_cdp` 连上去,
复用它的上下文与登录态。

为什么这样改 —— Playwright 自启的 Chromium 带有明显自动化特征,会被豆包风控识别并限流:

| 特征 | Playwright launch | 真实 Chrome(CDP 模式) |
| --- | --- | --- |
| `navigator.webdriver` | `true`(需 stealth 脚本硬改) | **原生 `false`** |
| CDP `Runtime.enable` 泄露 | 有(结构性缺陷) | 无(patchright 进一步加固) |
| 无头指纹 | 明显 | 真实有头 |
| 扩展 / WebGL / Canvas 指纹 | 缺失 | 与真人浏览器一致 |
| 登录态 | 需导出并注入 cookies | 留在 profile 里,浏览器自己管理 |

> 这也是「同一账号在 Edge 里正常、换成 Playwright 就被限流,连新账号也被限流」的根因 ——
> 问题出在**浏览器指纹**层面,不是账号本身。

### 账号模型

**1 个账号 = 1 个 Chrome 实例 = 1 个 User Data Dir = 1 个固定 IP**

```json
{
  "accounts": [
    {
      "account_id": "account_1",
      "cdp_port": 9222,
      "user_data_dir": ".profiles/account_1",
      "proxy": { "server": "http://host:8080", "username": "user1", "password": "pass1" },
      "status": "active"
    }
  ]
}
```

> `cookies` 字段不再使用 —— 登录态由各 Chrome profile 自己保管。
> 旧格式的 `accounts.json` 会被自动迁移(补上 `cdp_port` 与 `user_data_dir`)。

---

## 目录结构

```
doubao-crawler/
├── config.py               # 配置:UA、代理、Redis、并发数、风控阈值
├── main.py                 # 入口:初始化、入队、启动 Worker
├── account_pool.py         # 账号池:健康分、冷却、固定代理绑定
├── sse_parser.py           # 豆包 SSE 流解析器
├── task_queue.py           # Redis 任务队列,支持断点续爬与死信
├── worker.py               # Worker 调度器,处理单个任务
├── screenshot.py           # 截图工具
├── degradation_detector.py # 降权检测与自动恢复
├── utils.py                # 通用工具:词表/账号加载、Markdown/结果落盘
├── requirements.txt
├── README.md
└── data/
    ├── words.txt           # 待查询词表,每行一个(支持 # 注释)
    ├── accounts.json       # 账号配置:cookies + 固定代理
    ├── markdown/           # 正文 Markdown(文件名为 task_id 的 MD5)
    ├── screenshots/        # 截图(文件名为 task_id 的 MD5)
    └── results.json        # 结构化结果(JSON Lines,每行一条)
```

---

## 技术栈

- Python 3.10+(本项目在 **Python 3.12.7** 上创建了 `.venv`)
- Playwright(async API)+ Chromium
- Redis(任务队列 + 账号池状态)
- asyncio + Semaphore 并发控制
- aiohttp(SSE 解析器直连模式,调试用)
- FastAPI + uvicorn(可选管理端)
- playwright-stealth(可选,增强隐蔽性)

---

## 安装

本项目已在本机创建虚拟环境 `.venv`(基于 `D:\applications\anaconda3\python.exe`,即系统 Python 3.12.7)。

```bash
# 1) 进入项目目录
cd /d D:\project\doubao-crawler

# 2) 激活虚拟环境(CMD)
.venv\Scripts\activate

# 或 PowerShell
.venv\Scripts\Activate.ps1

# 3) 安装依赖
pip install -r requirements.txt

# 4) 安装 Chromium 浏览器内核
playwright install chromium
```

---

## 准备数据

### 1. `data/words.txt`

每行一个词,空行与 `#` 开头的注释行会被忽略,重复词自动去重:

```
人工智能
大语言模型
检索增强生成
```

### 2. `data/accounts.json`

每个账号绑定一个**固定代理 IP**(长期不切换,频繁换 IP 会直接触发降权):

```json
{
  "accounts": [
    {
      "account_id": "account_1",
      "cookies": [
        { "name": "sessionid", "value": "……", "domain": ".doubao.com", "path": "/" }
      ],
      "proxy": {
        "server": "http://127.0.0.1:18080",
        "username": "",
        "password": ""
      }
    }
  ]
}
```

> `cookies` 直接从已登录的浏览器导出,至少包含 `sessionid` / `sessionid_ss`。
> `proxy` 可为 `null`(表示直连),但**强烈建议**每个账号配独立固定代理。

### 3. 启动 Redis

```bash
# Docker 方式
docker run -d --name redis-doubao -p 6379:6379 redis:7-alpine
```

---

## 账号管理

账号按 `data/accounts.json` 里的条目加载,**每个账号都可以绑定自己的固定代理**。

**不带参数运行即进入交互菜单**(推荐,不用记命令):

```bat
.venv\Scripts\python.exe scripts\add_account.py
```

运行后看到:

```text
==========================================================
 豆包账号管理
==========================================================
  0) 添加账号
  1) 删除账号
  2) 查看账号列表
  q) 退出
----------------------------------------------------------
 请选择 [0/1/2/q]:
```

| 菜单项 | 说明 |
| --- | --- |
| **0) 添加账号** | 弹出浏览器扫码登录,自动导出 cookies 写入 `data/accounts.json` |
| **1) 删除账号** | 列出账号 → 按编号或 `account_id` 删除(可选一并清理其浏览器 profile) |
| **2) 查看账号列表** | 显示账号、cookie 数、登录态、绑定代理 |

也可以直接用命令行(与菜单等价,便于脚本化):

```bat
.venv\Scripts\python.exe scripts\add_account.py --id account_2
.venv\Scripts\python.exe scripts\add_account.py --id account_2 --proxy http://127.0.0.1:18080
.venv\Scripts\python.exe scripts\add_account.py --id account_2 --proxy http://user:pass@1.2.3.4:8080
.venv\Scripts\python.exe scripts\add_account.py --list
.venv\Scripts\python.exe scripts\add_account.py --delete account_2 --purge-profile --yes
```

代理支持两种写法:

| 写法 | 适用 |
| --- | --- |
| `http://host:port` | 直连型代理 |
| `http://user:pass@host:port` | 带用户名密码的代理 |

行为说明:

- 每个账号使用**独立的浏览器 profile**(`.profiles/<account_id>/`),互不串号;
- 同一个 `--id` 重复执行会**覆盖**该账号的旧 cookie(用于重新登录);
- 脚本检测到 `sessionid` 后自动导出并写回 `data/accounts.json`;
- 下次运行 `main.py` / `run.bat` 时会自动加载新账号,无需其它配置。

> **为什么不能直接导入 Edge 的登录态?** 实测:Edge 153 的 cookie 是
> v20(App-Bound Encryption),外部脚本无法解密;而 Edge 又禁止在默认数据目录上
> 开启远程调试,无法让 Playwright 直接复用。所以必须让浏览器自己完成登录,
> 再用 `context.storage_state()` 导出(由浏览器自身解密)。

### 多账号要点

| 事项 | 说明 |
| --- | --- |
| **固定代理** | 每个账号长期绑定同一出口 IP,频繁切换会直接触发降权 |
| **数量** | 账号越多,单账号压力越小;5000 词/天建议至少 3-5 个账号轮换 |
| **健康分隔离** | 每个账号独立计分与冷却,某个账号被限流不影响其它账号 |
| **并发** | `MAX_CONCURRENT_PAGES` 是**全局** Page 上限,不随账号数自动放大 |

---

## 一键运行(Windows)

项目根目录的 **`run.bat`** 可直接双击运行,它会自动完成:

1. 检查 / 创建虚拟环境 `.venv`
2. 检查 / 安装依赖(`playwright`、`redis`、`aiohttp` 等)
3. 检查 Redis —— 若未运行,自动启动**内置开发用 Redis**(`scripts/dev_redis.py`,基于 fakeredis)
4. 环境自检(`scripts/check_env.py`):登录态、词表、浏览器内核
5. 启动采集

所有命令行参数会原样透传给 `main.py`:

```bat
run.bat                        :: 使用默认词表与账号
run.bat --workers 3            :: 3 个并发 Worker
run.bat --clear                :: 先清空旧队列
run.bat --words data\words.txt :: 指定词表
run.bat --persistent           :: 复用已扫码登录的持久化 profile
```

只做环境自检:

```bat
.venv\Scripts\python.exe scripts\check_env.py
```

> **生产部署**请使用真实 Redis(例如 `docker run -d -p 6379:6379 redis:7-alpine`),
> 并准备**多个账号 + 固定代理**。内置的 fakeredis 仅用于本机试用:进程退出后
> 队列数据即丢失,也不适用于多机部署。

---

## 运行

### 日常流程(CDP 模式)

Chrome 实例是**长期驻留**的:启动一次后可以一直跑,采集脚本随用随停。

```bat
:: 1) 启动各账号的 Chrome 实例(首次安装、或重启机器后执行)
.venv\Scripts\python.exe scripts\launch_chrome.py --all

:: 2) 在弹出的窗口里确认已登录豆包(未登录就登录一下,登录态会留在 profile 里)

:: 3) 开始采集
.venv\Scripts\python.exe main.py

:: 常用参数
python main.py --clear             # 先清空旧队列再开始
python main.py --words D:\x.txt    # 指定词表
python main.py --no-enqueue        # 只消费已有任务(断点续爬)
python main.py --no-autolaunch     # 实例没运行时不自动拉起
python main.py --workers 3         # 并发 Worker 数
python main.py --admin             # 附带启动 FastAPI 管理端(http://127.0.0.1:8000)
```

`main.py` 启动时会:

1. 自动拉起**未运行**的 Chrome 实例(可用 `--no-autolaunch` 关闭);
2. 对每个实例跑 **CDP 验证门**(连通性 / 上下文 / 登录态),并打印可用账号数;
3. 用可用账号采集,任务之间保持**随机间隔**。

> **采集期间不要关闭 Chrome 窗口**。CDP 断开重连是正常的,但 Chrome 进程必须活着 ——
> 登录态就在它的 profile 里。

### 检查状态

```bat
.venv\Scripts\python.exe scripts\check_env.py               :: 全量自检(依赖/Redis/实例/登录态/词表)
.venv\Scripts\python.exe scripts\launch_chrome.py --status  :: 只看实例运行状态
.venv\Scripts\python.exe scripts\manage_accounts.py --check :: 检查登录态与出口 IP
```

管理端接口:`GET /health`、`GET /stats`、`GET /accounts`、`GET /accounts/{id}`。

### 常用环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` | `127.0.0.1` / `6379` / `0` | Redis 连接 |
| `DOUBAO_CHROME_PATH` | 自动探测 | 浏览器 exe 路径(Chrome → Edge) |
| `CDP_PORT_BASE` | `9222` | 各账号调试端口起始值(依次递增) |
| `MAX_CONCURRENT_PAGES` | `1` | 并发 Page 数 |
| `TASK_INTERVAL_MIN` / `MAX` | `60` / `180` | 任务之间的**随机**间隔区间(秒) |
| `ACCOUNT_INTERVAL_MIN` / `MAX` | `60` / `180` | 同账号两次查询的随机最小间隔(秒) |
| `ACTION_DELAY_MIN` / `MAX` | `0.5` / `1.8` | 模拟真人操作的随机停顿(秒) |
| `RECOVERY_REQUESTS` | `5` | 冷却恢复后的"温柔期"请求次数 |
| `RECOVERY_INTERVAL_FACTOR` | `2.0` | 温柔期的间隔倍数 |
| `TASK_TIMEOUT` | `280` | 单个任务整体超时(秒) |
| `UI_RESPONSE_TIMEOUT` | `220` | 等待 completion 响应的最长秒数 |
| `RATE_LIMIT_COOLING_TIME` | `1800` | 命中限流后的账号冷却秒数 |
| `ACCOUNT_MIN_HEALTH` | `60` | 可调度的最低健康分 |
| `NO_REF_STREAK_LIMIT` | `3` | 连续无参考链接多少次进入冷却 |
| `COOLING_TIME` / `HEALTH_COOLING_TIME` | `3600` / `7200` | 常规 / 低健康分的冷却秒数 |
| `DOUBAO_CLOSE_CHROME` | `0` | 设为 `1` 则采集结束后关闭 Chrome 实例 |
| `DOUBAO_AUTO_LAUNCH` | `1` | 设为 `0` 则不自动拉起实例 |

---

## 风控与稳定性设计

豆包对账号与 IP 有风控,**轻度异常时表现为静默降权**:接口仍返回 200、正文照常,但不再附带参考链接。仅看 HTTP 状态码无法察觉,因此本项目以"参考链接返回率"作为核心健康指标。

### 健康分

```
健康分 = 成功率 × 40 + 参考链接率 × 60
```

参考链接权重更高(60%),因为能否返回参考链接才真正反映风控状态。

### 冷却与恢复

| 触发条件 | 处置 |
| --- | --- |
| 连续 3 次无参考链接 | 移入 `pool:cooling`,冷却 **1 小时**,连续计数清零 |
| 健康分 < 40 | 移入 `pool:cooling`,冷却 **2 小时** |
| 巡检发现:查询数 ≥ 10 且参考链接率 < 30% | 移入 `pool:cooling`,冷却 **2 小时** |
| 冷却到期 | 自动重新激活,健康分重置为 60 |

### 其它要点

- **Cookie 注入时序**:必须先 `context.add_cookies()` 再 `page.goto()`。顺序颠倒会让豆包 JS 以游客态初始化,后续请求缺少签名参数而被拦截。
- **固定代理**:每个账号绑定固定出口 IP,长期不切换。这是**最关键**的一条。
- **账号间隔**:同一账号两次查询至少间隔 90 秒(`MIN_ACCOUNT_INTERVAL`),任务之间同样留 90 秒(`TASK_INTERVAL`)。
- **反检测**:已注入覆盖 `navigator.webdriver` 的脚本,并在安装后自动启用 `playwright-stealth`。

### 排查:一跑就提示 rate limited

限流响应长这样:

```json
{"error_code":710022004,"error_msg":"rate limited",
 "extra":{"decision":{"from":"shark_admin","type":"verify",
                      "region":"cn","verify_scene":"doubao_message_web"}}}
```

**它是按请求来源(出口 IP + 设备指纹)打的,而不是按账号打的**。所以典型症状是:
**再加账号、再换 profile 都没用,一跑就限流**。

按下面顺序排查:

**1) 确认是不是 IP 级限流**

```bat
.venv\Scripts\python.exe scripts\check_ip.py
```

记下当前出口 IP。如果所有账号都是 `proxy=None`,它们实际共用这一条出口,
请求一多,整条 IP 就会被标记。

**2) 换一条出口 IP**

- **手机热点**最省事:手机开热点 → 电脑连上 → 出口 IP 变成移动网络的地址;
- 重启光猫有时也能换(取决于运营商是否动态分配);
- 长期方案是买代理(见第 4 点)。

换完再跑一次 `check_ip.py`,确认「出口 IP」确实变了。

**3) 换完 IP 后温柔起跑**

新 IP 也别一上来就跑几十个词:

- 先跑 **1 个词**试水,成功再放量;
- 单 IP 建议把间隔调大:设置环境变量 `TASK_INTERVAL=300`(5 分钟),或直接改 `config.py`;
- 同一账号最好在**新 IP 下重新登录一次**(`scripts/add_account.py`),让账号与新出口绑定 ——
  账号 IP 突然跳变本身也会引起风控注意。

**4) 长期方案:代理 + 多账号**

单 IP 做不了 5000 词/天,算一笔账就清楚:

| 配置 | 完成 5000 词所需时间 |
| --- | --- |
| 1 账号 / 1 IP / 90 秒间隔 | **约 125 小时**(远超一天) |
| 5 账号 / 5 IP / 每条 3 并发 / 10 秒间隔 | 约 1 小时 |

所以要把 `MAX_CONCURRENT_PAGES` 和 `TASK_INTERVAL` 调回去,前提是**先有足够的独立 IP**。
仓库默认值(1 并发 + 90 秒)是为**稳定跑通验证**设定的,不是为吞吐量。

给账号绑定代理:

```bat
.venv\Scripts\python.exe scripts\add_account.py --id account_1 --proxy http://user:pass@host:port
```

---

## 任务队列与断点续爬

状态流转:`PENDING → RUNNING → COMPLETED`。

| 队列 | 作用 |
| --- | --- |
| `queue:pending` | 待处理(LPUSH 入队,RPOP 出队,FIFO) |
| `queue:running` | 处理中(元素带 `started_at`,用于超时判定) |
| `queue:done` | 已完成(仅保留最近 10000 条) |
| `queue:dead_letter` | 超过 `MAX_RETRY`(默认 3)次的任务 |

出队与确认都由 **Lua 脚本**在 Redis 端原子完成,Worker 崩溃不会丢任务。
启动时 `recover()` 会把 `queue:running` 中超过 `RUNNING_TIMEOUT`(默认 180 秒)的任务重新入队。

---

## 输出格式

`data/results.json` 为 **JSON Lines**,每行一条记录:

```json
{"task_id":"…","word":"人工智能","account_id":"account_1","success":true,"has_reference":true,"reference_count":3,"reference_links":[{"url":"…","title":"…","snippet":"…"}],"image_urls":[],"markdown_file":"D:\\project\\doubao-crawler\\data\\markdown\\….md","screenshot_file":"D:\\project\\doubao-crawler\\data\\screenshots\\….png","elapsed":11.4,"created_at":1737000000.0}
```

配套产物:正文 Markdown 存于 `data/markdown/`,截图存于 `data/screenshots/`,文件名均为 `task_id` 的 MD5,避免非法字符。

---

## 关于截图实现的重要说明

规格要求通过 `page.evaluate(fetch(...))` 获取 SSE。需要指出的是:这种方式**不会驱动豆包前端的状态机**,因此页面 DOM 中通常没有渲染好的回复,直接截图会得到空白页。

本项目采取的策略是:

1. 按规格在页面内 `fetch` 获取 SSE 文本并解析(签名由页面 JS 计算);
2. 截图前先做一次短超时(5 秒)的渲染检测;
3. 若未渲染,则把解析得到的 Markdown 与参考链接注入一个带 `data-testid='message-content'` 的容器,再 `full_page=True` 截图——保证**截图内容与解析结果一致**。

如需"由豆包前端自己渲染"的截图,应改为 UI 交互模式(定位输入框填入词、点击发送按钮,并监听 `page.on("response")` 捕获页面自身发出的 SSE)。该模式依赖豆包 DOM 选择器,稳定性低于 fetch 模式,故未默认启用。

---

## 实测校准记录

以下结论来自在本机(Windows + Playwright + 真实豆包账号)实际跑通的过程,与最初规格有多处差异,已按实测修正代码:

| 项目 | 规格/初版假设 | 实测结果 |
| --- | --- | --- |
| 接口路径 | `/samantha/chat/completion` | **`/chat/completion`** |
| 请求方式 | 页面内 `page.evaluate(fetch)` | **必须走 UI 交互**:URL 上带 `a_bogus`、`msToken`、`device_id`、`fp` 等由页面 JS 动态计算的签名参数,裸 `fetch` 无法构造 |
| 正文位置 | `message.content_block` 文本块 | **`CHUNK_DELTA` 事件的顶层 `{"text": "..."}`**(纯增量,须顺序拼接、不可去重) |
| 参考链接 | `message.reference_links` / `search_results` | **`search_query_result_block.results[].text_card.{title,url}`**(`block_type: 10025`) |
| SSE 分隔 | `\n\n` | **`\r\n\r\n`** |
| 响应编码 | UTF-8 | 响应体存在 **cp1252 双重编码**,需按字节反推还原(见 `utils.repair_mojibake`) |
| 事件类型 | 7 种 | 另有 **`STREAM_ERROR`**(风控/限流)、`STREAM_TIMEOUT_CONTROL` |

### 风控与限流

豆包会以 SSE 事件形式返回限流:

```
event: STREAM_ERROR
data: {"error_code":710022004,"error_msg":"rate limited",
       "extra":{"decision":{"type":"verify","verify_scene":"doubao_message_web"}}}
```

`DoubaoSseParser.is_rate_limited()` 会识别该状态,`worker` 随即把账号移入冷却
(`RATE_LIMIT_COOLING_TIME`,默认 30 分钟),而不是当作普通失败累加健康分。
**实测表明短时间密集请求(约 20 次)会触发账号级限流,且持续 10 分钟以上**——
生产上务必配置多账号 + 固定代理,并把并发控制在 3 个 Page 以内。

### 登录态获取

- **Edge 不可用**:Edge 153 的 cookie 是 v20(App-Bound Encryption),外部脚本无法解密;
  且 Edge 禁止在默认数据目录上开启远程调试(Playwright 内部用 `--remote-debugging-pipe`)。
  用 junction / profile 副本绕过时,浏览器拿不到 ABE 密钥,会**丢弃**这些 cookie
  (实测会把原 profile 的 cookie 清空,务必先备份 `Cookies` + `Local State`)。
- **可行路径**:用 `login_browser.py` 弹出 Playwright 自带 Chromium 扫码登录,再用
  `context.storage_state()` 导出 cookies(浏览器自身解密),写入 `data/accounts.json`,
  之后即可用本项目的标准流程(独立 context + 注入 cookies)运行。

---

## CDP 模式注意事项

### 必须遵守

1. **Chrome 实例保持运行** —— 不要关闭用于采集的 Chrome 窗口。CDP 断开重连是正常的,但 Chrome 进程必须活着。
2. **一个账号一个 Chrome 实例** —— 不要让多个账号共用一个实例,每个账号需要独立 User Data Dir 和调试端口。
3. **一个账号一个固定 IP** —— 用代理的**粘性会话**确保账号与 IP 长期绑定(见下)。
4. **请求间隔随机化** —— 固定间隔本身就是自动化特征,本项目已内置随机化 + 拟人化停顿。
5. **先 1 个词试水** —— 新账号或新 IP 下先跑 1 个词,确认不限流再放量。

### 监控指标

| 指标 | 阈值 / 查看方式 |
| --- | --- |
| 参考链接返回率 | 低于 30% 说明账号可能被降权 |
| 出口 IP | `scripts\manage_accounts.py --check` |
| 限流事件 | 日志中出现 `STREAM_ERROR` 且 `error_code: 710022004` |
| Chrome 实例状态 | `scripts\launch_chrome.py --status` |

### 代理与粘性会话

代理**必须在 Chrome 启动时**设置(`--proxy-server`),CDP 连上之后无法再修改。
需要认证的代理,Chrome 命令行不支持内联凭据 —— 请改用免认证代理、PAC 脚本或代理认证扩展。

给账号绑定代理:

```bat
.venv\Scripts\python.exe scripts\manage_accounts.py --add account_1 --proxy http://user:pass@host:port
```

多账号务必使用**粘性会话**(sticky session),让同一账号长期走同一出口 IP:

```json
{ "server": "http://residential-proxy:8080", "username": "user-session-abc123", "password": "pass" }
```

其中 `session-abc123` 是会话 ID —— 不同账号用不同 session,才能做到 **1 IP = 1 账号**。

---

## 已知限制

- `build_payload()`(仅 `fetch` 模式使用)中的请求体结构随豆包前端版本变化,请以实际抓包为准。
- UI 交互模式依赖豆包 DOM:输入框当前是 `[contenteditable='true']`(tiptap/ProseMirror),
  发送用 `Enter`。若页面改版需相应调整 `EDITOR_SELECTORS`。
- 参考链接只在豆包**触发联网检索**时才有;常识性问题(如"人工智能")不会搜索,`reference_count` 为 0 属正常现象。
- `CHUNK_DELTA` 增量流偶有缺字(实测开头几个字可能缺失),这是数据源特性而非解析缺陷。


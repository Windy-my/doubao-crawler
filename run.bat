@echo off
chcp 936 >nul
setlocal

cd /d "%~dp0"

echo.
echo  ==========================================================
echo    doubao-crawler  一键运行 (CDP 模式)
echo  ==========================================================
echo.

set "PY=%~dp0.venv\Scripts\python.exe"

REM ---------- 1/6 虚拟环境 ----------
if not exist "%PY%" (
    echo  [1/6] 未找到虚拟环境,正在创建 .venv ...
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo  [X] 系统 PATH 中没有 python,无法自动创建虚拟环境。
        echo      请先安装 Python 3.10+,然后在项目目录执行:
        echo          python -m venv .venv
        echo.
        pause
        exit /b 1
    )
    python -m venv ".venv"
    if errorlevel 1 (
        echo  [X] 创建虚拟环境失败。
        pause
        exit /b 1
    )
)
echo  [1/6] 虚拟环境就绪: .venv
echo.

REM ---------- 2/6 依赖 ----------
"%PY%" -c "import playwright, redis, aiohttp" >nul 2>nul
if errorlevel 1 (
    echo  [2/6] 安装项目依赖,首次运行需要几分钟 ...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo  [X] 依赖安装失败。可尝试国内源:
        echo      .venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)
echo  [2/6] 依赖就绪
echo.

REM ---------- 3/6 Redis ----------
"%PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',6379))==0 else 1)" >nul 2>nul
if errorlevel 1 (
    echo  [3/6] 未检测到 Redis,正在启动内置开发用 Redis ...
    "%PY%" -c "import fakeredis" >nul 2>nul
    if errorlevel 1 (
        echo         安装 fakeredis ...
        "%PY%" -m pip install "fakeredis[lua]" >nul 2>nul
    )
    start "doubao-dev-redis" /min "%PY%" "%~dp0scripts\dev_redis.py"
    ping -n 6 127.0.0.1 >nul
    "%PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',6379))==0 else 1)" >nul 2>nul
    if errorlevel 1 (
        echo  [X] 开发用 Redis 启动失败,请手动启动 Redis 后重试。
        pause
        exit /b 1
    )
    echo  [3/6] 已启动开发用 Redis  127.0.0.1:6379
) else (
    echo  [3/6] Redis 已就绪  127.0.0.1:6379
)
echo.

REM ---------- 4/6 启动 Chrome 实例 ----------
echo  [4/6] 检查并启动各账号的 Chrome 实例 ...
"%PY%" "%~dp0scripts\launch_chrome.py" --all
echo.

REM ---------- 5/6 环境自检 ----------
"%PY%" "%~dp0scripts\check_env.py"
if errorlevel 1 (
    echo.
    echo  [X] 请按上面的提示处理后重新运行本脚本。
    echo      最常见的阻塞项:某个账号的 Chrome 还没登录豆包,
    echo      请在对应的 Chrome 窗口里完成登录后重试。
    pause
    exit /b 1
)
echo.

REM ---------- 本次测试参数:间隔阶梯(试探最小安全请求间隔)----------
REM 每 DOUBAO_INTERVAL_STEP 个任务降一档:60 -> 45 -> 30 -> 20 -> 12 -> 6 -> 3 秒
REM 想恢复成 100-150 秒随机间隔,把下面两行 set 注释掉即可。
set "DOUBAO_INTERVAL_LADDER=60,45,30,20,12,6,3"
set "DOUBAO_INTERVAL_STEP=7"
REM 测试期间禁用"连续无参考链接"冷却:本次只测请求间隔,
REM 而行情类问题本身可能不返回参考链接,不应因此中断测试(日志仍会记录)。
set "NO_REF_STREAK_LIMIT=999"
set "DOUBAO_STOP_ON_RATE_LIMIT=1"
REM 超时放宽到 480 秒(新闻类问题检索耗时长)
REM 新闻/行情类问题联网检索耗时长,放宽超时
set "UI_RESPONSE_TIMEOUT=480"
set "TASK_TIMEOUT=540"

REM ---------- 6/6 启动采集 ----------
echo  [6/6] 启动采集,按 Ctrl+C 可中断,未完成任务保留在队列中可续跑
echo        注意:采集期间请不要关闭用于采集的 Chrome 窗口
echo.
"%PY%" main.py %*

echo.
echo  运行结束。Chrome 实例保持运行,下次可直接复用登录态。
pause
endlocal

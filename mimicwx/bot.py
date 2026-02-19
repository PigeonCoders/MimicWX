"""
MimicWX 核心引擎

桥接 wxauto (wx.py) 和 OneBotv11 (onebot.py)：
- 初始化微信连接
- 启动 WS 连接到 Yunzai
- 消息轮询循环：wxauto → OneBotv11 事件推送
- Action 回调：OneBotv11 → wxauto 发送
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time

from loguru import logger

from mimicwx.config import Config
from mimicwx.preflight import preflight
from mimicwx.wx import WxBot
from mimicwx.onebot import OneBotClient


class MimicBot:
    """
    MimicWX 核心引擎。

    用法:
        bot = MimicBot("config.yaml")
        bot.start()
    """

    def __init__(self, config_path: str = "config.yaml") -> None:
        self.cfg = Config.from_yaml(config_path)
        self.wx = WxBot()
        self.ob = OneBotClient(
            self.wx,
            url=self.cfg.ws_url,
            self_id=self.cfg.self_id,
            heartbeat_interval=self.cfg.heartbeat_interval,
            reconnect_delay=self.cfg.reconnect_delay,
        )
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []

        # 加载微信→QQ映射
        if self.cfg.wx_qq_map:
            self.ob.idmap.load_map(self.cfg.wx_qq_map)

    def start(self) -> None:
        """启动 MimicWX（阻塞运行）"""
        self._setup_logger()

        logger.info("=" * 50)
        logger.info("  MimicWX v0.3.0 — 微信适配器")
        logger.info("=" * 50)

        # 1. 启动前预检（版本检测 + 登录检查 + 更新绕过）
        preflight()

        # 2. 初始化微信
        self.wx.init()

        # 3. 添加监听
        if self.cfg.listen_all:
            logger.info("监听所有最近会话:")
            self.wx.listen_all()
            # 也加上手动配置的聊天（ChatWnd 可能不在会话列表中）
            if self.cfg.listen_chats:
                self.wx.listen(self.cfg.listen_chats)
        elif self.cfg.listen_chats:
            logger.info(f"添加监听 ({len(self.cfg.listen_chats)} 个):")
            self.wx.listen(self.cfg.listen_chats)
        else:
            logger.warning("未配置 listen_chats 或 listen_all")

        # 排水：丢弃 AddListenChat 时加载的历史消息
        self.wx.flush()

        # 4. 启动
        self._running = True

        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            self.wx.close()
            logger.info("MimicWX 已停止")

    async def _main(self) -> None:
        """异步主循环"""
        self._loop = asyncio.get_running_loop()

        # 注册信号
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self._shutdown)
            except NotImplementedError:
                # Windows 不支持 add_signal_handler
                # 用 signal 模块 + call_soon_threadsafe 跨线程安全调用
                signal.signal(
                    sig,
                    lambda s, f: self._loop.call_soon_threadsafe(self._shutdown),
                )

        # 启动 WS 连接任务
        ws_task = asyncio.create_task(self.ob.connect_loop())

        # 启动消息轮询任务
        poll_task = asyncio.create_task(self._poll_loop())

        self._tasks = [ws_task, poll_task]

        logger.info(
            f"🚀 已启动 | WS: {self.cfg.ws_url} | "
            f"轮询间隔: {self.cfg.poll_interval}s"
        )

        # 等待任务完成
        try:
            done, pending = await asyncio.wait(
                self._tasks,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if exc:
                    logger.error(f"任务异常: {exc}")
        except asyncio.CancelledError:
            pass
        finally:
            for task in self._tasks:
                task.cancel()
            # 等待 task 真正结束
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _shutdown(self) -> None:
        """停止所有任务"""
        if not self._running:
            # 第二次 Ctrl+C → 强制退出
            logger.warning("强制退出")
            os._exit(1)
        logger.info("正在停止...")
        self._running = False
        for task in self._tasks:
            task.cancel()

    async def _poll_loop(self) -> None:
        """消息轮询循环"""
        loop = asyncio.get_event_loop()
        poll_count = 0
        consecutive_errors = 0

        while self._running:
            try:
                # 定期任务
                poll_count += 1
                periodic = []
                # 刷新监听列表：每 15 轮（~15s）
                if poll_count % 15 == 0 and self.cfg.listen_all:
                    periodic.append(loop.run_in_executor(None, self.wx.refresh_listen))
                # 检查好友申请：每 60 轮（~60s），UI 操作较重
                if poll_count % 60 == 0 and self.cfg.auto_accept_friend:
                    periodic.append(loop.run_in_executor(None, self.wx.accept_new_friends))
                if periodic:
                    await asyncio.gather(*periodic)

                messages = await loop.run_in_executor(None, self.wx.poll)
                consecutive_errors = 0  # 成功，重置计数

                for msg in messages:
                    if not self.ob.connected:
                        # WS 未连接 → 缓存到队列
                        self.ob.buffer_message(msg)
                        continue

                    logger.info(
                        f"📨 {'群' if msg.is_group else '私'}消息 "
                        f"[{msg.chat_name}] {msg.sender}: {msg.content[:50]}"
                    )
                    self.ob.push_message(msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"轮询异常 ({consecutive_errors}次): {e}")

                # 连续失败 → 检查微信是否崩溃
                if consecutive_errors >= 5:
                    await self._try_recover(loop)
                    consecutive_errors = 0

            try:
                await asyncio.sleep(self.cfg.poll_interval)
            except asyncio.CancelledError:
                break

    async def _try_recover(self, loop: asyncio.AbstractEventLoop) -> None:
        """检测微信进程状态，必要时重建连接"""
        alive = await loop.run_in_executor(None, self.wx.is_alive)
        if alive:
            logger.warning("🔄 微信进程存活，尝试重建 wxauto 连接...")
            try:
                await loop.run_in_executor(None, self.wx.reinit)
                logger.success("✅ wxauto 连接已恢复")
            except Exception as e:
                logger.error(f"重建失败: {e}")
        else:
            logger.error("💀 微信进程已退出，等待重新启动...")
            # 等待微信进程恢复（最多 120 秒）
            for _ in range(24):
                if not self._running:
                    return
                await asyncio.sleep(5)
                alive = await loop.run_in_executor(None, self.wx.is_alive)
                if alive:
                    logger.info("🔄 微信已重新启动，等待初始化...")
                    await asyncio.sleep(5)
                    try:
                        await loop.run_in_executor(None, self.wx.reinit)
                        logger.success("✅ 微信重连成功")
                    except Exception as e:
                        logger.error(f"重连失败: {e}")
                    return
            logger.error("❌ 微信未在 120 秒内重启")

    def _setup_logger(self) -> None:
        """配置 loguru"""
        logger.remove()
        logger.add(
            sys.stderr,
            level=self.cfg.log_level,
            format=(
                "<green>{time:HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
                "<level>{message}</level>"
            ),
        )
        logger.add(
            "logs/mimicwx_{time:YYYY-MM-DD}.log",
            level="DEBUG",
            rotation="1 day",
            retention="7 days",
            encoding="utf-8",
        )

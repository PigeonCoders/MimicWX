"""
wxauto 封装层

提供微信消息收发的统一接口：
- 初始化 WeChat 实例
- 添加监听聊天 + 轮询新消息
- 快速发送文本（支持独立聊天窗口 ChatWnd + 主窗口两种模式）
- 发送图片/文件

引擎：weixin-auto (wxauto 3.9.11.17)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import psutil
import win32gui
from loguru import logger
from wxauto import WeChat
from wxauto.elements import ChatWnd
from wxauto.utils import FindWindow, SetClipboardText


# ── 数据模型 ─────────────────────────────────────────────


@dataclass
class WxMessage:
    """轮询到的微信消息"""

    chat_name: str
    sender: str
    content: str
    msg_type: str = "text"
    is_group: bool = False
    timestamp: float = field(default_factory=time.time)
    raw: Any = None


# ── WxBot ────────────────────────────────────────────────

# 不监听的系统聊天
_LISTEN_BLACKLIST = {"搜索", "", "微信团队", "文件传输助手", "腾讯新闻", "微信运动", "微信支付", "订阅号"}


class WxBot:
    """
    wxauto 操作封装。

    支持两种聊天窗口模式：
    - ChatWnd：独立聊天窗口（双击打开的）
    - 主窗口：嵌在微信主界面中的聊天
    """

    def __init__(self) -> None:
        self._wx: WeChat | None = None
        self._self_name: str = ""
        self._current_chat: str | None = None
        self._listening: set[str] = set()
        self._ui_lock = threading.Lock()
        self._chat_wnds: dict[str, ChatWnd] = {}  # 缓存独立聊天窗口
        self._first_poll = True  # 跳过第一次轮询（丢弃历史消息）
        self._processed_ids: set = set()  # 消息 ID 去重
        self._error_count = 0  # 连续错误计数，用于触发自动恢复

    # ── 生命周期 ─────────────────────────────────────

    def init(self) -> None:
        logger.info("正在连接微信 (引擎: wxauto)...")
        self._wx = WeChat()
        self._self_name = self._wx.nickname
        self._current_chat = None
        self._chat_wnds.clear()
        logger.success(f"微信已连接 — 账号: {self._self_name}")

    def reinit(self) -> None:
        logger.info("重建 WeChat 实例...")
        self._wx = WeChat()
        self._current_chat = None
        self._chat_wnds.clear()
        for name in list(self._listening):
            try:
                self._wx.AddListenChat(who=name)
            except Exception:
                pass

    def close(self) -> None:
        self._wx = None
        self._current_chat = None
        self._chat_wnds.clear()
        logger.info("wxauto 已关闭")

    def is_alive(self) -> bool:
        """检查微信进程是否还在运行"""
        for p in psutil.process_iter(['name']):
            try:
                if p.info['name'] and p.info['name'].lower() == 'wechat.exe':
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    # ── 身份信息 ─────────────────────────────────────

    def get_self_info(self) -> dict[str, str]:
        return {"name": self._self_name, "id": self._self_name}

    @property
    def self_name(self) -> str:
        return self._self_name

    # ── 监听 ─────────────────────────────────────────

    @staticmethod
    def _find_chat_wnds() -> list[str]:
        """用 Win32 API 快速扫描所有独立聊天窗口名称。

        比 uiautomation 遍历整个桌面快几十倍。
        """
        names: list[str] = []

        def _enum_cb(hwnd, _):
            try:
                cls = win32gui.GetClassName(hwnd)
                if cls == 'ChatWnd' and win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title:
                        names.append(title)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_enum_cb, None)
        return names

    def listen(self, chat_names: list[str]) -> None:
        for name in chat_names:
            if name in self._listening:
                continue
            try:
                self._wx.AddListenChat(who=name)
                self._listening.add(name)
                logger.info(f"  ✓ 监听: {name}")
            except Exception as e:
                logger.error(f"  ✗ 监听失败 {name}: {e}")

    def listen_all(self) -> None:
        """监听所有最近会话 + 所有已打开的独立聊天窗口"""
        names: list[str] = []

        # 1. 主窗口会话列表
        try:
            sessions = self._wx.GetSessionList(True)
            names = [k for k in sessions.keys()
                     if k not in _LISTEN_BLACKLIST and k != self._self_name]
        except Exception as e:
            logger.debug(f"获取会话列表: {e}")

        # 2. 扫描独立聊天窗口（Win32 EnumWindows，极快）
        for name in self._find_chat_wnds():
            if (name not in _LISTEN_BLACKLIST
                    and name != self._self_name
                    and name not in names):
                names.append(name)

        logger.info(f"发现 {len(names)} 个会话:")
        self.listen(names)

    def refresh_listen(self) -> None:
        """刷新监听列表（会话列表 + 独立窗口）"""
        new_names: list[str] = []

        # 主窗口会话
        try:
            sessions = self._wx.GetSessionList(True)
            for name in sessions.keys():
                if (name not in _LISTEN_BLACKLIST
                        and name != self._self_name
                        and name not in self._listening):
                    new_names.append(name)
        except Exception:
            pass

        # 独立聊天窗口
        for name in self._find_chat_wnds():
            if (name not in _LISTEN_BLACKLIST
                    and name != self._self_name
                    and name not in self._listening
                    and name not in new_names):
                new_names.append(name)

        for name in new_names:
            try:
                self._wx.AddListenChat(who=name)
                self._listening.add(name)
                logger.info(f"  + 新增监听: {name}")
            except Exception:
                pass

    def accept_new_friends(self) -> list[str]:
        """自动同意好友申请，返回新加的好友名称列表"""
        accepted: list[str] = []

        # 只锁 wxauto UI 操作
        with self._ui_lock:
            try:
                new_friends = self._wx.GetNewFriends()
            except Exception as e:
                logger.debug(f"检查好友申请: {e}")
                return accepted

            for friend in new_friends:
                try:
                    friend.Accept()
                    accepted.append(friend.name)
                    logger.info(f"✅ 已同意好友申请: {friend.name}")
                except Exception as e:
                    logger.error(f"同意好友申请失败: {e}")

        # 添加监听（锁外执行，不阻塞发送）
        for name in accepted:
            try:
                self._wx.AddListenChat(who=name)
                self._listening.add(name)
            except Exception:
                pass

        return accepted

    def flush(self) -> None:
        """排水：丢弃已有的消息，防止历史消息被当作新消息"""
        try:
            data = self._wx.GetListenMessage()
            count = sum(len(msgs) for msgs in (data or {}).values())
            if count:
                logger.info(f"  🗑️ 丢弃 {count} 条历史消息")
        except Exception:
            pass

    def poll(self) -> list[WxMessage]:
        """轮询所有监听聊天的新消息"""
        results: list[WxMessage] = []
        with self._ui_lock:
            try:
                raw_data = self._wx.GetListenMessage()
                self._error_count = 0  # 成功后重置
            except Exception as e:
                self._error_count += 1
                logger.error(f"GetListenMessage 失败 ({self._error_count}次): {e}")
                if self._error_count >= 3:
                    logger.warning("🔄 连续失败，尝试自动恢复...")
                    try:
                        self.reinit()
                        self._error_count = 0
                    except Exception as re:
                        logger.error(f"自动恢复失败: {re}")
                return results
            if not raw_data:
                raw_data = {}

        # 以下在锁外处理（不阻塞发送）

        # 第一次轮询：丢弃所有结果（历史消息）
        if self._first_poll:
            self._first_poll = False
            count = sum(len(msgs) for msgs in raw_data.values())
            for msgs in raw_data.values():
                for msg in msgs:
                    msg_id = msg.id
                    if msg_id:
                        self._processed_ids.add(msg_id)
            if count:
                logger.info(f"🗑️ 丢弃 {count} 条历史消息")
            return results

        if not raw_data:
            return results
        for chat_wnd, raw_msgs in raw_data.items():
            chat_name = chat_wnd.who
            if not raw_msgs:
                continue
            for raw_msg in raw_msgs:
                msg_id = raw_msg.id
                if msg_id:
                    if msg_id in self._processed_ids:
                        continue
                    self._processed_ids.add(msg_id)

                msg = self._convert(raw_msg, chat_name)
                if msg is not None:
                    results.append(msg)

        # 防止 set 无限增长
        if len(self._processed_ids) > 5000:
            self._processed_ids = set(list(self._processed_ids)[-2000:])

        return results

    # ── 发送 ─────────────────────────────────────────

    def send_text(self, chat_name: str, text: str) -> bool:
        """发送文本消息（带重试）"""
        max_retries = 2
        for attempt in range(max_retries + 1):
            with self._ui_lock:
                t0 = time.time()

                # === 快速发送（ChatWnd 或主窗口） ===
                try:
                    self._fast_send(chat_name, text)
                    elapsed = (time.time() - t0) * 1000
                    logger.info(f"⚡ 快速发送 → {chat_name} ({elapsed:.0f}ms)")
                    return True
                except Exception as e:
                    logger.warning(f"快速发送失败: {type(e).__name__}: {e}")

                # === 回退：标准 SendMsg ===
                try:
                    self._wx.SendMsg(text, who=chat_name)
                    self._current_chat = chat_name
                    elapsed = (time.time() - t0) * 1000
                    logger.info(f"📤 标准发送 → {chat_name} ({elapsed:.0f}ms)")
                    return True
                except Exception as e:
                    logger.error(f"发送失败 → {chat_name} (attempt {attempt+1}): {e}")

            # 重试前清理状态
            if attempt < max_retries:
                self._current_chat = None
                self._chat_wnds.pop(chat_name, None)
                time.sleep(0.5 * (attempt + 1))  # 退避等待
                logger.info(f"🔄 重试发送 ({attempt + 2}/{max_retries + 1})...")

        self._current_chat = None
        return False

    def _get_chat_wnd(self, chat_name: str) -> ChatWnd | None:
        """获取独立聊天窗口（有缓存 + 句柄有效性检测）"""
        # 先检查缓存的句柄是否仍然有效
        if chat_name in self._chat_wnds:
            cached = self._chat_wnds[chat_name]
            try:
                cached_hwnd = cached.UiaAPI.NativeWindowHandle
                if not win32gui.IsWindow(cached_hwnd):
                    logger.debug(f"ChatWnd 句柄已失效: {chat_name}")
                    del self._chat_wnds[chat_name]
            except Exception:
                self._chat_wnds.pop(chat_name, None)

        hwnd = FindWindow(name=chat_name, classname='ChatWnd')
        if not hwnd:
            self._chat_wnds.pop(chat_name, None)
            return None

        if chat_name not in self._chat_wnds:
            logger.debug(f"创建 ChatWnd: {chat_name} (hwnd={hwnd})")
            self._chat_wnds[chat_name] = ChatWnd(chat_name, self._wx.language)
        return self._chat_wnds[chat_name]

    def _fast_send(self, chat_name: str, text: str) -> bool:
        """
        快速发送。

        模式1: ChatWnd（独立窗口） → 直接用缓存的 editbox
        模式2: 主窗口 → ChatWith + EditControl
        """
        t0 = time.time()

        # ── 模式1：独立聊天窗口 ──
        chat_wnd = self._get_chat_wnd(chat_name)
        if chat_wnd is not None:
            # 只在窗口不可见时才激活，避免不必要的 _show() 开销
            hwnd = FindWindow(name=chat_name, classname='ChatWnd')
            if hwnd and not win32gui.IsWindowVisible(hwnd):
                chat_wnd._show()

            editbox = chat_wnd.editbox
            if not editbox.HasKeyboardFocus:
                editbox.Click(simulateMove=False)

            self._clipboard_send(editbox, text, chat_name)

            elapsed = (time.time() - t0) * 1000
            logger.debug(f"ChatWnd 模式: {elapsed:.0f}ms")
            return True

        # ── 模式2：主窗口 ──
        if self._current_chat != chat_name:
            self._wx._show()
            self._wx.ChatWith(chat_name, timeout=2)
            self._current_chat = chat_name
            time.sleep(0.15)

        self._wx._show()
        editbox = self._wx.ChatBox.EditControl(searchDepth=5)

        if not editbox.HasKeyboardFocus:
            editbox.Click(simulateMove=False)

        self._clipboard_send(editbox, text, chat_name)

        elapsed = (time.time() - t0) * 1000
        logger.debug(f"主窗口模式: {elapsed:.0f}ms")
        return True

    def _clipboard_send(self, editbox: Any, text: str, chat_name: str) -> None:
        """粘贴文本到输入框并回车发送"""
        deadline = time.time() + 5
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"粘贴超时: {chat_name}")
            SetClipboardText(text)
            editbox.SendKeys("{Ctrl}v")
            if editbox.GetValuePattern().Value:
                break
            time.sleep(0.05)  # 避免 CPU 空转
        editbox.SendKeys("{Enter}")

    def send_image(self, chat_name: str, path: str) -> bool:
        with self._ui_lock:
            try:
                self._wx.SendFiles(filepath=path, who=chat_name)
                self._current_chat = chat_name
                logger.info(f"🖼️ 发送图片 → {chat_name}")
                return True
            except Exception as e:
                logger.error(f"发送图片失败 → {chat_name}: {e}")
                return False

    def send_file(self, chat_name: str, path: str) -> bool:
        with self._ui_lock:
            try:
                self._wx.SendFiles(filepath=path, who=chat_name)
                self._current_chat = chat_name
                logger.info(f"📎 发送文件 → {chat_name}")
                return True
            except Exception as e:
                logger.error(f"发送文件失败 → {chat_name}: {e}")
                return False

    # ── 消息转换 ─────────────────────────────────────

    def _convert(self, raw_msg: Any, chat_name: str) -> WxMessage | None:
        """将 wxauto 消息对象转换为 WxMessage。

        wxauto 消息类型及属性：
        - FriendMessage: .sender(str), .sender_remark(str), .content(str), .id, .type='friend'
        - SelfMessage:   .sender(str), .content(str), .id, .type='self'
        - SysMessage:    .sender(str), .content(str), .id, .type='sys'
        - TimeMessage:   .sender(str), .content(str), .id, .type='time'
        - RecallMessage: .sender(str), .content(str), .id, .type='recall'
        """
        try:
            # 过滤系统/时间/撤回/自己的消息
            msg_type_raw = raw_msg.type
            if msg_type_raw in ("sys", "time", "recall", "self"):
                return None

            sender = str(raw_msg.sender)
            if sender in ("Self", "self", self._self_name):
                return None

            content = str(raw_msg.content)
            if not content:
                return None

            # 消息类型映射
            msg_type = "text"
            if msg_type_raw == "friend":
                msg_type = "text"  # 好友消息默认文本
            # wxauto 3.9.11.17 的消息 type 只有上述几种
            # 图片消息在 content 中体现为文件路径

            # 群聊检测：sender != chat_name → 群聊
            is_group = sender != chat_name

            return WxMessage(
                chat_name=chat_name,
                sender=sender,
                content=content,
                msg_type=msg_type,
                is_group=is_group,
                raw=raw_msg,
            )
        except Exception as e:
            logger.debug(f"消息转换失败: {e}")
            return None

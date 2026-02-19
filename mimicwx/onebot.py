"""
OneBotv11 WebSocket 客户端

单文件实现完整的 OneBotv11 协议层：
- 作为 WS 客户端连接到 Yunzai 的 /OneBotv11 端点
- 上行：推送 lifecycle / heartbeat / message 事件
- 下行：接收 Action 请求 → 调用 WxBot 执行 → 返回响应
- ID 映射：微信名称 ↔ 数字 ID（hash）
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

import websockets
from loguru import logger

if TYPE_CHECKING:
    from mimicwx.wx import WxBot


# ── ID 映射 ──────────────────────────────────────────────


class IdMap:
    """微信名称 ↔ 数字 ID 的双向映射ï¼支持持久化ï¼"""

    def __init__(self, map_file: str = "wx_qq_map.json") -> None:
        self._name2id: dict[str, int] = {}
        self._id2name: dict[int, str] = {}
        self._map_file = Path(map_file)
        self._load_file()

    def _load_file(self) -> None:
        """从 JSON 文件加载持久化映射"""
        if self._map_file.exists():
            try:
                data = json.loads(self._map_file.read_text(encoding="utf-8"))
                for wx_name, qq_id in data.items():
                    nid = int(qq_id)
                    self._name2id[wx_name] = nid
                    self._id2name[nid] = wx_name
                logger.info(f"加载 {len(data)} 条 QQ 绑定")
            except Exception as e:
                logger.error(f"加载 {self._map_file} 失败: {e}")

    def _save_file(self) -> None:
        """保存持久化映射到 JSON 文件"""
        # 只保存显式绑定的（排除 hash 生成的）
        persistent = {}
        for name, nid in self._name2id.items():
            if nid != abs(hash(name)) % (10**10):
                persistent[name] = nid
        try:
            self._map_file.write_text(
                json.dumps(persistent, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"保存 {self._map_file} 失败: {e}")

    def load_map(self, wx_qq_map: dict[str, int | str]) -> None:
        """预加载 微信名 → QQ号 的固定映射"""
        for wx_name, qq_id in wx_qq_map.items():
            nid = int(qq_id)
            self._name2id[wx_name] = nid
            self._id2name[nid] = wx_name
            logger.debug(f"ID 绑定: {wx_name} → {nid}")

    def bind(self, wx_name: str, qq_id: int) -> None:
        """绑定微信名与 QQ 号并持久化"""
        self._name2id[wx_name] = qq_id
        self._id2name[qq_id] = wx_name
        self._save_file()
        logger.info(f"🔗 QQ 绑定: {wx_name} → {qq_id}")

    def to_id(self, name: str) -> int:
        if not name:
            return 0
        if name in self._name2id:
            return self._name2id[name]
        nid = abs(hash(name)) % (10**10)
        self._name2id[name] = nid
        self._id2name[nid] = name
        return nid

    def to_name(self, nid: int) -> str | None:
        return self._id2name.get(nid)

    def resolve(self, id_or_name: Any) -> str:
        """将数字 ID 或名称字符串统一解析为微信名称"""
        if isinstance(id_or_name, str) and not id_or_name.isdigit():
            return id_or_name
        numeric = int(id_or_name) if isinstance(id_or_name, str) else id_or_name
        name = self.to_name(numeric)
        if name:
            return name
        logger.warning(f"无法解析 ID: {id_or_name}")
        return str(id_or_name)

    def get_qq(self, wx_name: str) -> int | None:
        """获取绑定的 QQ 号（只返回显式绑定的）"""
        nid = self._name2id.get(wx_name)
        if nid is not None and nid != abs(hash(wx_name)) % (10**10):
            return nid
        return None


# ── 事件构造 ─────────────────────────────────────────────


def _lifecycle_event(self_id: str, sub_type: str = "connect") -> dict:
    return {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": sub_type,
    }


def _heartbeat_event(self_id: str) -> dict:
    return {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "status": {"online": True, "good": True},
        "interval": 30000,
    }


def _message_event(
    self_id: str,
    msg_id: int,
    user_id: int,
    nickname: str,
    message: list[dict],
    raw_text: str,
    *,
    group_id: int | None = None,
) -> dict:
    """构造 OneBotv11 message 事件"""
    is_group = group_id is not None
    evt: dict[str, Any] = {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "message",
        "message_type": "group" if is_group else "private",
        "sub_type": "normal" if is_group else "friend",
        "message_id": msg_id,
        "user_id": user_id,
        "message": message,
        "raw_message": raw_text,
        "font": 0,
        "sender": {
            "user_id": user_id,
            "nickname": nickname,
            "sex": "unknown",
            "age": 0,
        },
    }
    if is_group:
        evt["group_id"] = group_id
        evt["sender"]["card"] = nickname
    return evt


# ── CQ Code 解析 ─────────────────────────────────────────

_CQ_RE = re.compile(r"\[CQ:(\w+?)(?:,(.*?))?\]")


def _parse_cq(raw: str) -> list[dict]:
    """CQ Code 字符串 → 消息段数组"""
    segments: list[dict] = []
    last = 0
    for m in _CQ_RE.finditer(raw):
        if m.start() > last:
            text = raw[last : m.start()]
            if text:
                segments.append({"type": "text", "data": {"text": text}})
        cq_type = m.group(1)
        data = {}
        if m.group(2):
            for kv in m.group(2).split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    data[k.strip()] = v.strip()
        segments.append({"type": cq_type, "data": data})
        last = m.end()
    if last < len(raw):
        text = raw[last:]
        if text:
            segments.append({"type": "text", "data": {"text": text}})
    return segments


# ── 消息段 → 发送动作 ────────────────────────────────────


def _msg_to_actions(
    message: list[dict] | str, idmap: IdMap
) -> list[dict[str, str]]:
    """
    将 OneBotv11 消息段数组转换为发送操作列表。

    Returns:
        [{"action": "text"|"image"|"file", "content": "..."}]
    """
    if isinstance(message, str):
        message = _parse_cq(message)

    actions: list[dict[str, str]] = []
    text_buf = ""

    for seg in message:
        seg_type = seg.get("type", "text")
        data = seg.get("data", {})

        if seg_type == "text":
            text_buf += data.get("text", "")
        elif seg_type == "image":
            if text_buf:
                actions.append({"action": "text", "content": text_buf})
                text_buf = ""
            file_ref = data.get("file", data.get("url", ""))
            if file_ref:
                actions.append({"action": "image", "content": file_ref})
        elif seg_type == "at":
            qq = data.get("qq", "")
            display = qq
            if str(qq).isdigit():
                name = idmap.to_name(int(qq))
                if name:
                    display = name
            text_buf += f"@{display} "
        elif seg_type == "face":
            text_buf += f"[表情{data.get('id', '')}]"
        elif seg_type == "file":
            if text_buf:
                actions.append({"action": "text", "content": text_buf})
                text_buf = ""
            fp = data.get("file", "")
            if fp:
                actions.append({"action": "file", "content": fp})
        elif seg_type in ("reply", "node"):
            pass  # 忽略回复/转发
        else:
            t = data.get("text", "")
            if t:
                text_buf += t

    if text_buf:
        actions.append({"action": "text", "content": text_buf})

    return actions


def _resolve_image(file_ref: str) -> str:
    """将 base64:// 图片引用 → 本地临时文件路径"""
    if file_ref.startswith("base64://"):
        raw = file_ref[len("base64://") :]
        img_data = base64.b64decode(raw)
        tmp_dir = os.path.join(tempfile.gettempdir(), "mimicwx_images")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, f"img_{id(img_data)}.png")
        with open(tmp_path, "wb") as f:
            f.write(img_data)
        return tmp_path
    return file_ref


# ── OneBotClient ─────────────────────────────────────────


class OneBotClient:
    """
    OneBotv11 WebSocket 客户端。

    连接到 Yunzai 的 /OneBotv11 端点，实现双向通信。
    """

    def __init__(
        self,
        wx: "WxBot",
        *,
        url: str = "ws://127.0.0.1:2536/OneBotv11",
        self_id: str = "MimicWX",
        heartbeat_interval: int = 30,
        reconnect_delay: int = 5,
    ) -> None:
        self.wx = wx
        self.url = url
        self.self_id = self_id
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_delay = reconnect_delay

        self.idmap = IdMap()
        self._ws: Any = None
        self._connected = False
        self._msg_counter = 0
        self._msg_buffer: deque[dict] = deque(maxlen=200)  # WS 断开时缓存

    # ── 对外接口 ─────────────────────────────────────

    async def connect_loop(self) -> None:
        """连接循环（带自动重连），适合作为 asyncio task 运行"""
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosedError as e:
                logger.warning(f"🔌 连接断开: {e}")
            except ConnectionRefusedError:
                logger.warning(
                    f"🔌 无法连接 Yunzai ({self.url})，"
                    f"{self.reconnect_delay}s 后重试..."
                )
            except Exception as e:
                logger.error(f"WebSocket 异常: {e}")
            finally:
                self._connected = False
                self._ws = None

            try:
                await asyncio.sleep(self.reconnect_delay)
            except asyncio.CancelledError:
                raise

    def push_message(self, msg: "WxMessage") -> None:
        """
        将微信消息推送为 OneBotv11 事件（由外部轮询循环调用）。

        内置命令（不转发到 Yunzai）：
        - /bindqq <QQ号>  绑定 QQ 号
        - /myqq            查看当前绑定
        """
        from mimicwx.wx import WxMessage  # noqa: F811

        # ── 内置命令拦截 ──
        text = msg.content.strip()
        if text.startswith("/bindqq "):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].isdigit():
                qq_id = int(parts[1])
                self.idmap.bind(msg.sender, qq_id)
                self.wx.send_text(msg.chat_name, f"✅ 绑定成功！\n微信: {msg.sender}\nQQ: {qq_id}")
            else:
                self.wx.send_text(msg.chat_name, "❌ 格式错误\n用法: /bindqq QQ号\n示例: /bindqq 123456789")
            return

        if text == "/myqq":
            qq = self.idmap.get_qq(msg.sender)
            if qq:
                self.wx.send_text(msg.chat_name, f"🔗 当前绑定\n微信: {msg.sender}\nQQ: {qq}")
            else:
                self.wx.send_text(msg.chat_name, f"❌ 未绑定 QQ\n用法: /bindqq QQ号")
            return

        # ── 正常消息转发 ──
        self._msg_counter += 1
        user_id = self.idmap.to_id(msg.sender)

        # 构造消息段
        if msg.msg_type == "image":
            segments = [{"type": "image", "data": {"file": msg.content}}]
        else:
            segments = [{"type": "text", "data": {"text": msg.content}}]

        # 群聊需要 group_id
        group_id = self.idmap.to_id(msg.chat_name) if msg.is_group else None

        event = _message_event(
            self.self_id,
            self._msg_counter,
            user_id,
            msg.sender,
            segments,
            msg.content,
            group_id=group_id,
        )

        self._send_nonblock(event)

    def buffer_message(self, msg: "WxMessage") -> None:
        """WS 断开时缓存消息事件"""
        from mimicwx.wx import WxMessage  # noqa: F811

        self._msg_counter += 1
        user_id = self.idmap.to_id(msg.sender)

        if msg.msg_type == "image":
            segments = [{"type": "image", "data": {"file": msg.content}}]
        else:
            segments = [{"type": "text", "data": {"text": msg.content}}]

        group_id = self.idmap.to_id(msg.chat_name) if msg.is_group else None

        event = _message_event(
            self.self_id,
            self._msg_counter,
            user_id,
            msg.sender,
            segments,
            msg.content,
            group_id=group_id,
        )
        self._msg_buffer.append(event)
        logger.debug(f"📦 消息已缓存 ({len(self._msg_buffer)}/{self._msg_buffer.maxlen})")

    async def _flush_buffer(self) -> None:
        """补发缓存的消息事件"""
        if not self._msg_buffer:
            return
        count = len(self._msg_buffer)
        logger.info(f"📤 补发 {count} 条缓存消息...")
        while self._msg_buffer:
            event = self._msg_buffer.popleft()
            await self._send(event)
            await asyncio.sleep(0.1)  # 避免洪水攻击
        logger.info(f"✅ {count} 条缓存消息已补发")

    @property
    def connected(self) -> bool:
        return self._connected

    # ── 内部实现 ─────────────────────────────────────

    async def _session(self) -> None:
        """一次完整的 WS 会话"""
        logger.info(f"🔗 连接 Yunzai: {self.url}")
        async with websockets.connect(self.url) as ws:
            self._ws = ws
            self._connected = True
            logger.success("✅ 已连接到 Yunzai")

            # 发送 lifecycle 事件（触发 Yunzai connect 流程）
            await self._send(_lifecycle_event(self.self_id))
            logger.debug("已发送 lifecycle/connect")

            # 补发断线期间缓存的消息
            await self._flush_buffer()

            # 启动心跳
            hb_task = asyncio.create_task(self._heartbeat())

            try:
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        asyncio.create_task(self._handle_action(data))
                    except json.JSONDecodeError:
                        logger.warning(f"非 JSON 消息: {str(raw)[:80]}")
                    except Exception as e:
                        logger.error(f"处理消息异常: {e}")
            finally:
                hb_task.cancel()

    async def _heartbeat(self) -> None:
        """定期心跳"""
        while self._connected:
            await asyncio.sleep(self.heartbeat_interval)
            await self._send(_heartbeat_event(self.self_id))

    async def _send(self, data: dict) -> None:
        if self._ws:
            try:
                await self._ws.send(json.dumps(data, ensure_ascii=False))
            except Exception as e:
                logger.error(f"WS 发送失败: {e}")
                self._connected = False

    def _send_nonblock(self, data: dict) -> None:
        """从同步上下文发送（用于轮询线程调用）"""
        if not self._connected or not self._ws:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(self._send(data), loop)
            else:
                loop.run_until_complete(self._send(data))
        except Exception as e:
            logger.error(f"非阻塞发送失败: {e}")

    # ── Action 处理 ──────────────────────────────────

    async def _handle_action(self, data: dict) -> None:
        """处理 Yunzai 下发的 Action"""
        action = data.get("action", "")
        params = data.get("params", {})
        echo = data.get("echo")

        t0 = time.time()
        if action.startswith("send_"):
            preview = ""
            for seg in params.get("message", []):
                if isinstance(seg, dict) and seg.get("type") == "text":
                    preview = seg.get("data", {}).get("text", "")[:40]
                    break
            logger.info(f"📥 Yunzai 回复 ({action}): {preview}...")

        # 在线程池中执行（wxauto 是同步阻塞的 UI 自动化）
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, self._dispatch_action, action, params, echo
        )

        if action.startswith("send_"):
            elapsed = (time.time() - t0) * 1000
            logger.info(f"📤 微信发送完成 ({elapsed:.0f}ms)")

        if self._ws:
            await self._send(response)

    def _dispatch_action(self, action: str, params: dict, echo: Any) -> dict:
        """同步 Action 分发器"""
        try:
            result = self._do_action(action, params)
            return self._ok(result, echo)
        except Exception as e:
            logger.error(f"Action 失败 [{action}]: {e}")
            return self._err(str(e), echo)

    def _do_action(self, action: str, params: dict) -> Any:
        """执行具体 Action"""
        match action:
            # ── 消息发送 ──
            case "send_msg":
                mt = params.get("message_type", "")
                if mt == "group" or "group_id" in params:
                    return self._act_send(params, "group_id")
                return self._act_send(params, "user_id")

            case "send_private_msg":
                return self._act_send(params, "user_id")

            case "send_group_msg":
                return self._act_send(params, "group_id")

            # ── 信息查询 ──
            case "get_login_info":
                info = self.wx.get_self_info()
                return {
                    "user_id": self.idmap.to_id(info.get("name", "")),
                    "nickname": info.get("name", "MimicWX"),
                }

            case "get_friend_list":
                return []

            case "get_group_list":
                return []

            case "get_group_info":
                gid = params.get("group_id", 0)
                name = self.idmap.resolve(gid)
                return {
                    "group_id": gid,
                    "group_name": name,
                    "member_count": 0,
                    "max_member_count": 500,
                }

            case "get_group_member_list":
                return []

            case "get_group_member_info":
                uid = params.get("user_id", 0)
                return {
                    "group_id": params.get("group_id", 0),
                    "user_id": uid,
                    "nickname": self.idmap.resolve(uid),
                    "card": "",
                    "role": "member",
                }

            case "get_stranger_info":
                uid = params.get("user_id", 0)
                return {
                    "user_id": uid,
                    "nickname": self.idmap.resolve(uid),
                    "sex": "unknown",
                    "age": 0,
                }

            case "get_status":
                return {"online": True, "good": True}

            case "get_version_info":
                return {
                    "app_name": "MimicWX",
                    "app_version": "0.3.0",
                    "protocol_version": "v11",
                }

            case "can_send_image":
                return {"yes": True}

            case "can_send_record":
                return {"yes": False}

            case "delete_msg" | "get_msg":
                return None

            case (
                "_set_model_show"
                | "get_guild_service_profile"
                | "get_online_clients"
                | "get_cookies"
                | "get_csrf_token"
                | "send_like"
                | "set_group_card"
                | "set_group_ban"
            ):
                return None

            case "get_guild_list" | "get_guild_channel_list":
                return []

            case _:
                logger.debug(f"不支持的 Action: {action}")
                return None

    def _act_send(self, params: dict, id_key: str) -> dict:
        """执行消息发送"""
        target_id = params.get(id_key, params.get("user_id", ""))
        chat_name = self.idmap.resolve(target_id)
        message = params.get("message", "")
        actions = _msg_to_actions(message, self.idmap)

        for act in actions:
            match act["action"]:
                case "text":
                    self.wx.send_text(chat_name, act["content"])
                case "image":
                    filepath = _resolve_image(act["content"])
                    self.wx.send_image(chat_name, filepath)
                case "file":
                    self.wx.send_file(chat_name, act["content"])

        self._msg_counter += 1
        return {"message_id": self._msg_counter}

    # ── 响应构造 ─────────────────────────────────────

    @staticmethod
    def _ok(data: Any, echo: Any = None) -> dict:
        r: dict = {"status": "ok", "retcode": 0, "data": data}
        if echo is not None:
            r["echo"] = echo
        return r

    @staticmethod
    def _err(msg: str, echo: Any = None) -> dict:
        r: dict = {"status": "failed", "retcode": 1400, "data": None, "message": msg}
        if echo is not None:
            r["echo"] = echo
        return r

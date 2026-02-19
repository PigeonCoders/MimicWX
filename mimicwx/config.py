"""
MimicWX 配置
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from loguru import logger


class Config(BaseModel):
    """MimicWX 配置（扁平结构，不做过度嵌套）"""

    log_level: str = "INFO"
    poll_interval: float = Field(default=1.0, description="消息轮询间隔（秒）")
    listen_chats: list[str] = Field(default_factory=list, description="监听的聊天列表")
    listen_all: bool = Field(default=False, description="监听所有最近聊天")
    auto_accept_friend: bool = Field(default=False, description="自动同意好友申请")

    # 微信名 → QQ号 映射（跨平台数据互通）
    wx_qq_map: dict[str, int] = Field(default_factory=dict, description="微信名→QQ号映射")

    ws_url: str = Field(
        default="ws://127.0.0.1:2536/OneBotv11",
        description="Yunzai OneBotv11 WebSocket 地址",
    )
    self_id: str = Field(default="MimicWX", description="Bot self_id")
    heartbeat_interval: int = Field(default=30, description="心跳间隔（秒）")
    reconnect_delay: int = Field(default=5, description="断线重连延迟（秒）")

    # ------------------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | Path = "config.yaml") -> "Config":
        p = Path(path)
        if not p.exists():
            logger.warning(f"配置文件 {p} 不存在，使用默认配置")
            return cls()
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.info(f"已加载配置: {p}")
        return cls(**data)

    def to_yaml(self, path: str | Path = "config.yaml") -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            yaml.dump(
                self.model_dump(),
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        logger.info(f"配置已保存: {p}")

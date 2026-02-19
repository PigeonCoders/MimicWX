# MimicWX

基于 [wxauto](https://github.com/cluic/wxauto) 的微信适配器，通过 OneBot v11 协议将微信接入 [TRSS-Yunzai](https://github.com/TimeRainStarSky/Yunzai)。

## 功能

- **消息收发**：文本、图片消息的接收与发送
- **群聊支持**：群消息监听、群名/发送者解析
- **好友管理**：自动同意好友申请、自动添加到监听列表
- **跨平台绑定**：微信名 ↔ QQ号 映射，共享 Yunzai 用户数据
- **性能优化**：Win32 窗口扫描、ChatWnd 独立窗口快速发送、UI 锁最小化
- **稳定性**：WeChat 崩溃自动恢复、窗口句柄验证、发送失败重试、消息缓冲队列

## 环境要求

- Windows 10/11
- Python 3.10+
- 微信 PC 客户端（已登录）
- TRSS-Yunzai（已启动 OneBotv11 WebSocket 服务）

## 安装

```bash
pip install wxauto websockets pydantic loguru pyyaml psutil pywin32
```

## 配置

编辑 `config.yaml`：

```yaml
listen_all: true              # 监听所有最近聊天
ws_url: "ws://127.0.0.1:2536/OneBotv11"
self_id: "MimicWX"
```

## 运行

```bash
python run.py
```

## 架构

```
WeChat ←→ wxauto ←→ MimicWX ←→ WebSocket ←→ Yunzai
              ↑                       ↑
         UI Automation          OneBot v11
```

| 文件 | 职责 |
|------|------|
| `run.py` | 入口：管理员提权 + 启动 |
| `mimicwx/bot.py` | 主循环：消息轮询、崩溃恢复 |
| `mimicwx/wx.py` | wxauto 封装：发送、监听、窗口管理 |
| `mimicwx/onebot.py` | OneBotv11 协议：WS 连接、事件/动作转换 |
| `mimicwx/preflight.py` | 启动前检查：版本绕过、环境验证 |
| `mimicwx/config.py` | 配置管理 |

## 许可

MIT

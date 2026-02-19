"""
MimicWX 启动入口

用法:
    python run.py                # 使用默认 config.yaml
    python run.py my_config.yaml # 指定配置文件
"""

import sys
import os
import ctypes


def _ensure_admin():
    """检查管理员权限，如果没有则通过 UAC 弹窗请求提升。"""
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        is_admin = False

    if is_admin:
        return  # 已经是管理员

    # 重新以管理员身份启动自身
    script = os.path.abspath(sys.argv[0])
    params = f'"{script}"'
    if len(sys.argv) > 1:
        params += " " + " ".join(f'"{a}"' for a in sys.argv[1:])

    # cwd 传入当前工作目录，确保路径一致
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, os.getcwd(), 1
    )
    # ShellExecuteW 返回值 > 32 表示成功
    if ret > 32:
        sys.exit(0)  # 新的管理员进程已启动，退出当前进程
    else:
        print("❌ 无法获取管理员权限，请右键以管理员身份运行")
        sys.exit(1)


def main() -> None:
    from mimicwx.bot import MimicBot

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    bot = MimicBot(config_path)
    bot.start()


if __name__ == "__main__":
    _ensure_admin()
    main()


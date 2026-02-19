"""
微信启动前预检

在 MimicBot 初始化 wxauto 之前执行：
1. 检测微信是否运行
2. 检测微信版本
3. 检测登录状态 → 未登录则执行更新绕过 + 等待登录
"""

from __future__ import annotations

import os
import time
import ctypes


from loguru import logger

# wxauto 期望的目标版本
TARGET_VERSION = "3.9.11.17"




# ── 工具函数 ─────────────────────────────────────────────


def _is_admin() -> bool:
    """检查是否以管理员权限运行"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _find_window(classname: str) -> int:
    """查找窗口句柄（不依赖 wxauto，直接调用 win32gui）"""
    import win32gui
    return win32gui.FindWindow(classname, None)


def _find_wechat_window_by_title() -> int:
    """通过窗口标题枚举查找微信窗口（兼容所有版本）"""
    import win32gui
    result = [0]

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title == "微信":
            result[0] = hwnd
    win32gui.EnumWindows(callback, None)
    return result[0]


def _get_path_by_hwnd(hwnd: int) -> str | None:
    """通过窗口句柄获取进程的可执行文件路径"""
    import win32process
    import psutil
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).exe()
    except Exception:
        return None


def _get_version_by_path(path: str) -> str | None:
    """通过文件路径获取版本号"""
    import win32api
    try:
        info = win32api.GetFileVersionInfo(path, '\\')
        return "{}.{}.{}.{}".format(
            win32api.HIWORD(info['FileVersionMS']),
            win32api.LOWORD(info['FileVersionMS']),
            win32api.HIWORD(info['FileVersionLS']),
            win32api.LOWORD(info['FileVersionLS']),
        )
    except Exception:
        return None


def _detect_wechat_major(hwnd: int) -> int:
    """通过可执行文件路径和版本号判断微信大版本 (3 or 4)"""
    path = _get_path_by_hwnd(hwnd)
    if not path:
        return 0
    version = _get_version_by_path(path)
    if version:
        try:
            return int(version.split('.')[0])
        except (ValueError, IndexError):
            pass
    # 根据 exe 名称推断：WeChat.exe = 3.x, Weixin.exe = 4.x
    basename = os.path.basename(path).lower()
    if basename == "wechat.exe":
        return 3
    if basename == "weixin.exe":
        return 4
    return 0


# ── 检测函数 ─────────────────────────────────────────────

# 微信 3.9.x 窗口类名
_WX3_MAIN_CLASS = 'WeChatMainWndForPC'
_WX3_LOGIN_CLASS = 'WeChatLoginWndForPC'

# 微信 4.x 窗口类名 (Qt-based)
_WX4_CLASSES = ('Qt51514QWindowIcon',)


def check_wechat_running() -> tuple[bool, bool, int]:
    """检测微信窗口状态。

    同时兼容微信 3.9.x 和 4.x。

    Returns:
        (is_main_wnd, is_login_wnd, hwnd):
        - is_main_wnd: 主窗口存在（已登录）
        - is_login_wnd: 登录窗口存在（未登录）
        - hwnd: 找到的窗口句柄
    """
    # 优先检测 3.9.x（wxauto 兼容版本）
    main_hwnd = _find_window(_WX3_MAIN_CLASS)
    if main_hwnd:
        return True, False, main_hwnd

    login_hwnd = _find_window(_WX3_LOGIN_CLASS)
    if login_hwnd:
        return False, True, login_hwnd

    # 检测 4.x（通过窗口标题枚举）
    qt_hwnd = _find_wechat_window_by_title()
    if qt_hwnd:
        # 4.x 窗口找到 → 视为已登录的主窗口
        # （4.x 的登录和主界面都在同一个窗口中）
        return True, False, qt_hwnd

    return False, False, 0


def check_wechat_version(hwnd: int) -> str | None:
    """读取微信版本号。"""
    path = _get_path_by_hwnd(hwnd)
    if not path:
        return None
    return _get_version_by_path(path)


def check_wechat_login() -> bool:
    """检测微信 3.9.x 是否已登录。

    只检测 WeChatMainWndForPC（wxauto 兼容窗口），
    不使用标题枚举，避免误检测到同时运行的 4.x 窗口。
    """
    return _find_window(_WX3_MAIN_CLASS) != 0


# ── 版本绕过 ─────────────────────────────────────────────


def _version_to_hex(version: str) -> int:
    """微信版本号转 hex。

    3.9.11.17 → 0x63090B11
    规则: (major + 0x60) << 24 | minor << 16 | patch << 8 | build
    """
    parts = [int(x) for x in version.split('.')]
    if len(parts) != 4:
        return 0
    return ((parts[0] + 0x60) << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


# 伪装目标版本号（固定值，用于绕过版本检测）
_FAKE_VERSION_HEX = 0xF254162E


def _bypass_memory_patch(hwnd: int) -> bool:
    """修改微信进程内存中的版本号，绕过「版本过低」登录限制。

    扫描 WeChatWin.dll 整个内存区域，找到所有版本号 hex 值并替换。
    需要管理员权限（OpenProcess + WriteProcessMemory）。
    """
    import win32process

    if not _is_admin():
        logger.warning("⚠️ 内存补丁需要管理员权限，请以管理员身份运行")
        return False

    # 获取微信 PID
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        logger.warning("无法获取微信 PID")
        return False

    # 获取当前版本的 hex 值
    path = _get_path_by_hwnd(hwnd)
    version = _get_version_by_path(path) if path else None
    if not version:
        logger.warning("无法获取微信版本号")
        return False

    old_hex = _version_to_hex(version)
    if old_hex == 0:
        logger.warning(f"版本号格式异常: {version}")
        return False

    logger.info(f"  版本 {version} → 0x{old_hex:08X}, 目标 → 0x{_FAKE_VERSION_HEX:08X}")

    kernel32 = ctypes.windll.kernel32

    # 打开进程（PROCESS_ALL_ACCESS）
    process_handle = kernel32.OpenProcess(0x1F0FFF, False, pid)
    if not process_handle:
        logger.warning(f"无法打开微信进程 (PID={pid})")
        return False

    try:
        # 查找 WeChatWin.dll 基址和大小
        dll_base, dll_size = _find_module_info(kernel32, pid, "WeChatWin.dll")
        if not dll_base:
            logger.warning("未找到 WeChatWin.dll 模块")
            return False

        logger.info(f"  WeChatWin.dll 基址: 0x{dll_base:X}, 大小: {dll_size // 1024 // 1024}MB")

        # 全内存扫描：读取整个 DLL 内存，搜索版本号
        old_bytes = old_hex.to_bytes(4, 'little')
        fake_bytes = _FAKE_VERSION_HEX.to_bytes(4, 'little')

        patched = 0
        already = 0
        chunk_size = 0x10000  # 64KB 分块读取
        buffer = ctypes.create_string_buffer(chunk_size + 4)  # +4 防止跨块边界
        bytes_read = ctypes.c_size_t()

        offset = 0
        while offset < dll_size:
            read_size = min(chunk_size + 4, dll_size - offset)
            ok = kernel32.ReadProcessMemory(
                process_handle,
                ctypes.c_void_p(dll_base + offset),
                buffer,
                read_size,
                ctypes.byref(bytes_read),
            )
            if not ok or bytes_read.value == 0:
                offset += chunk_size
                continue

            data = buffer.raw[:bytes_read.value]

            # 搜索未修改的版本号
            pos = 0
            while True:
                idx = data.find(old_bytes, pos)
                if idx == -1 or idx >= chunk_size:
                    break
                addr = dll_base + offset + idx
                if _write_uint32(kernel32, process_handle, addr, _FAKE_VERSION_HEX):
                    patched += 1
                    logger.debug(f"  ✓ 已修改 0x{addr:X} (偏移 +0x{offset + idx:X})")
                pos = idx + 4

            # 统计已修改的位置
            pos = 0
            while True:
                idx = data.find(fake_bytes, pos)
                if idx == -1 or idx >= chunk_size:
                    break
                already += 1
                pos = idx + 4

            offset += chunk_size

        if patched > 0:
            logger.info(f"✅ 内存补丁完成: {patched} 处已修改")
            return True
        elif already > 0:
            logger.info(f"✅ 内存补丁已生效 ({already} 处已是目标值)")
            return True
        else:
            logger.warning("未在 WeChatWin.dll 中找到版本号，补丁失败")
            return False

    finally:
        kernel32.CloseHandle(process_handle)


def _find_module_info(kernel32, pid: int, module_name: str) -> tuple[int | None, int]:
    """获取进程中指定模块的基址和大小。"""
    from ctypes import wintypes

    class MODULEENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("th32ModuleID", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("GlblcntUsage", wintypes.DWORD),
            ("ProccntUsage", wintypes.DWORD),
            ("modBaseAddr", ctypes.POINTER(wintypes.BYTE)),
            ("modBaseSize", wintypes.DWORD),
            ("hModule", wintypes.HMODULE),
            ("szModule", ctypes.c_char * 256),
            ("szExePath", ctypes.c_char * 260),
        ]

    # TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32
    for attempt in range(5):
        snapshot = kernel32.CreateToolhelp32Snapshot(0x18, pid)
        if snapshot and snapshot != -1:
            break
        time.sleep(1)
    else:
        return None, 0

    me32 = MODULEENTRY32()
    me32.dwSize = ctypes.sizeof(MODULEENTRY32)

    try:
        if kernel32.Module32First(snapshot, ctypes.byref(me32)):
            while True:
                try:
                    name = me32.szModule.decode('utf-8', errors='ignore')
                    if module_name.lower() in name.lower():
                        base = ctypes.cast(me32.modBaseAddr, ctypes.c_void_p).value
                        size = me32.modBaseSize
                        return base, size
                except Exception:
                    pass
                if not kernel32.Module32Next(snapshot, ctypes.byref(me32)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    return None, 0


def _read_uint32(kernel32, handle, address: int) -> int | None:
    """读取进程内存中的 32 位整数。"""
    buffer = ctypes.c_uint32()
    bytes_read = ctypes.c_size_t()
    ok = kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(address),
        ctypes.byref(buffer), 4, ctypes.byref(bytes_read),
    )
    return buffer.value if ok and bytes_read.value == 4 else None


def _write_uint32(kernel32, handle, address: int, value: int) -> bool:
    """写入 32 位整数到进程内存（自动修改页保护）。"""
    old_protect = ctypes.c_ulong()
    # PAGE_EXECUTE_READWRITE
    kernel32.VirtualProtectEx(
        handle, ctypes.c_void_p(address), 4, 0x40,
        ctypes.byref(old_protect),
    )
    buffer = ctypes.c_uint32(value)
    bytes_written = ctypes.c_size_t()
    ok = kernel32.WriteProcessMemory(
        handle, ctypes.c_void_p(address),
        ctypes.byref(buffer), 4, ctypes.byref(bytes_written),
    )
    # 恢复保护
    kernel32.VirtualProtectEx(
        handle, ctypes.c_void_p(address), 4, old_protect.value,
        ctypes.byref(ctypes.c_ulong()),
    )
    return ok and bytes_written.value == 4


def apply_update_bypass(hwnd: int) -> None:
    """应用版本绕过（内存补丁）。"""
    logger.info("🛡️ 正在应用版本绕过...")

    ok = _bypass_memory_patch(hwnd)
    if ok:
        logger.info("🛡️ 版本绕过成功")
    else:
        logger.warning("🛡️ 版本绕过失败，登录时可能提示「版本过低」")


# ── 等待登录 ─────────────────────────────────────────────


def wait_for_login(timeout: float = 300, poll_interval: float = 2.0) -> bool:
    """等待用户完成微信登录。

    Args:
        timeout: 最大等待时间（秒），默认 5 分钟
        poll_interval: 轮询间隔（秒）

    Returns:
        True 登录成功，False 超时
    """
    logger.info(f"⏳ 等待微信登录... (超时: {timeout:.0f}s)")
    logger.info("   请在微信窗口中扫码或点击「进入微信」")

    t0 = time.time()
    dots = 0
    while time.time() - t0 < timeout:
        if check_wechat_login():
            logger.success("✅ 微信已登录!")
            # 登录后等待一小段时间让窗口完全加载
            time.sleep(2)
            return True
        dots = (dots + 1) % 4
        print(f"\r  等待登录{'.' * dots}{' ' * (4 - dots)}", end="", flush=True)
        time.sleep(poll_interval)

    print()  # 换行
    logger.error(f"❌ 等待登录超时 ({timeout:.0f}s)")
    return False


# ── 主入口 ─────────────────────────────────────────────


def preflight() -> None:
    """启动前预检主入口。

    检查顺序：
    1. 微信是否运行 → 未运行则等待
    2. 检测版本 → 版本不匹配则警告
    3. 检测登录状态 → 未登录则执行更新绕过 + 等待登录
    """
    logger.info("─" * 40)
    logger.info("  🔍 微信启动前预检")
    logger.info("─" * 40)

    # ── 1. 检测微信窗口 ──
    is_main, is_login, hwnd = check_wechat_running()

    if not is_main and not is_login:
        logger.warning("⚠️ 未检测到微信窗口，请先启动微信!")
        logger.info("   等待微信启动...")

        # 等待微信窗口出现
        t0 = time.time()
        while time.time() - t0 < 120:
            is_main, is_login, hwnd = check_wechat_running()
            if is_main or is_login:
                break
            time.sleep(2)
        else:
            raise RuntimeError("微信未在 120 秒内启动，请手动打开微信后重试")

    # ── 2. 检测版本 ──
    version = check_wechat_version(hwnd)
    major = _detect_wechat_major(hwnd)

    if version:
        logger.info(f"  📋 微信版本: {version}")

        # 微信 4.x 与 wxauto 3.9 不兼容
        if major >= 4:
            logger.error(
                "❌ 检测到微信 4.x，wxauto 不兼容!\n"
                "   wxauto 3.9.11.17 仅支持微信 3.9.x 版本\n"
                "   \n"
                "   解决方法:\n"
                "   1. 卸载当前微信 4.x\n"
                "   2. 下载并安装微信 3.9.11:\n"
                "      https://github.com/tom-snow/wechat-windows-versions/releases/tag/v3.9.11.17\n"
                "   3. 安装后先不要登录，直接运行 MimicWX（会自动禁止更新）\n"
                "   4. 再登录微信"
            )
            raise RuntimeError(
                f"微信版本不兼容: 当前 {version}，需要 3.9.x。"
                f"请安装微信 3.9.11.17 后重试。"
            )

        if version != TARGET_VERSION:
            logger.warning(
                f"⚠️ 版本不完全匹配: 当前 {version}, 期望 {TARGET_VERSION}\n"
                f"   wxauto 可能功能异常，建议安装精确匹配版本"
            )
        else:
            logger.info(f"  ✅ 版本匹配 wxauto 期望")
    else:
        logger.warning("  ⚠️ 无法读取微信版本号")

    # ── 3. 检测登录状态 ──
    if is_main:
        logger.info("  ✅ 微信已登录")
        logger.info("─" * 40)
        return

    # 未登录 → 内存补丁 + 等待登录
    logger.info("  ⏸️ 微信未登录（检测到登录窗口）")
    apply_update_bypass(hwnd)
    logger.info("  👉 请扫码登录微信")

    if not wait_for_login():
        raise RuntimeError("微信登录超时，请登录后重新运行 MimicWX")

    logger.info("─" * 40)

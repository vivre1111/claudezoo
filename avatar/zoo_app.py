# -*- coding: utf-8 -*-
"""
Claude Zoo 桌面版 —— 把小怪物钉在屏幕角落。

不用 Electron，不用 pip 装任何东西：借 Windows 自带 Edge 的 --app 模式开一个
无地址栏的窗口，再用 Win32 API 把它摆到位、置顶。

    python zoo_app.py                     左下角，430 宽 x 差不多站满整屏高（顶上留一张卡片的空当）
    python zoo_app.py --pos br            右下角
    python zoo_app.py --size 380x900      自定尺寸
    python zoo_app.py --no-top            不置顶
    python zoo_app.py --alpha 80          清晰时的透明度（40~100，默认 90）
    python zoo_app.py --ghost 10          鼠标在本屏时淡到多透（0~90，默认 15）
    python zoo_app.py --no-ghost          透明度不跟鼠标走，固定 --alpha
    python zoo_app.py --no-follow         不自动躲到另一块屏
    python zoo_app.py --opaque            关掉半透明
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from http.server import ThreadingHTTPServer

import zoo_play
import zoo_server

# 单位是 CSS 像素，不是物理像素。150% 缩放的屏上，460 物理像素的窗口页面里
# 只有 307 CSS 像素宽，卡片会挤成一团。所以按 CSS 尺寸定义，实际开窗时乘缩放。
# H 故意给得很大：反正 _rect_for 会拿它跟屏幕高度取 min，真正说了算的是下面的 TOP_GAP。
W, H, MARGIN = 430, 5000, 14
TOP_GAP = 110               # 顶上留出来的空当，大约一张卡片的高度（CSS 像素）
MIN_H = 50                 # 折叠后只剩标题栏的高度（CSS 像素）
POS = "br"                 # 默认贴右下角（每块屏都一样）
TOPMOST = True
ALPHA = 90                 # 鼠标不在本屏 / 指着它时的不透明度
GHOST = 15                 # 鼠标在本屏但没指它时淡到这个不透明度（几乎全透明）
DYNAMIC = True             # 透明度是否跟着鼠标走
FOLLOW = True              # 鼠标进了它那块屏，就躲到另一块屏
GRACE = 1.0               # 躲之前的宽限秒数（够你径直移过去抓住它）
TITLE_HINT = "ClaudeZoo"
BRAND = "Claude Zoo"
PROFILE_NAME = "ClaudeZoo"
URL_SUFFIX = ""
_STATE = "max"             # 当前是展开还是折叠
_WA = None                 # 当前所在显示器的工作区 (l,t,r,b)，None=主屏
_WA_SCALE = None           # 当前所在显示器的缩放，None=主屏缩放
_CORNER = None             # 当前贴哪个角，None=用 POS。外接屏上改贴右下角

user32 = ctypes.WinDLL("user32", use_last_error=True)


def dpi_aware():
    """不声明 DPI 感知的话，缩放屏上拿到的坐标全是错的（这台机是 150%）。"""
    try:
        # PER_MONITOR_AWARE_V2
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def work_area():
    """主屏的可用区域——已经把任务栏挖掉了，所以贴边不会被挡。"""
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    r = RECT()
    if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):   # SPI_GETWORKAREA
        return 0, 0, 1920, 1080
    return r.left, r.top, r.right, r.bottom


def target_rect():
    """返回物理像素的目标矩形（Win32 API 用的都是物理像素）。"""
    return _rect_for(_STATE)


def _rect_for(state):
    l, t, r, b = _WA if _WA else work_area()      # 贴当前所在显示器的工作区
    s = _WA_SCALE if _WA_SCALE else scale()       # 按那块屏自己的 DPI 算尺寸
    w = min(int(W * s), r - l - MARGIN * 2)
    # 顶部只留 TOP_GAP（约一张卡片高），不是对称的 MARGIN——所以窗口比原来高得多
    full = min(int(H * s), b - t - MARGIN - int(TOP_GAP * s))
    h = int(MIN_H * s) if state == "min" else full   # 折叠时只留标题栏那点高
    corner = _CORNER if _CORNER else POS
    x = l + MARGIN if corner[1] == "l" else r - w - MARGIN
    # 底部锚定：折叠/展开都贴着同一条底边，往上长
    y = b - h - MARGIN if corner[0] == "b" else t + MARGIN
    return x, y, w, h


def find_edge():
    for p in (os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
              os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe")):
        if p and os.path.exists(p):
            return p
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
            return winreg.QueryValueEx(k, None)[0]
    except Exception:
        return None


WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def scale():
    """屏幕缩放倍率。Edge 命令行里的坐标是 DIP，Win32 API 用物理像素，得换算。"""
    try:
        return (user32.GetDpiForSystem() or 96) / 96.0
    except Exception:
        return 1.0


def find_window(root_pid, deadline):
    """等 Edge 把窗口开出来。认标题 + 认进程，避免抓到别的 Edge 窗口。"""
    found = []
    pid_set = set()

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pid_set:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if TITLE_HINT in buf.value:
            found.append(hwnd)
            return False
        return True

    proc = WNDENUMPROC(cb)
    while time.time() < deadline:
        # 每轮都重新算子进程：Edge 的窗口进程是启动之后才 fork 出来的，
        # 只在开头快照一次的话，窗口所属 pid 永远不在集合里，找一辈子也找不到。
        pid_set = child_pids(root_pid)
        found.clear()
        user32.EnumWindows(proc, 0)
        if found:
            return found[0]
        time.sleep(0.25)
    return None


def child_pids(root_pid):
    """Edge 是多进程的，窗口挂在哪个子进程上不一定，把整棵树都算上。"""
    import ctypes as C
    k32 = C.WinDLL("kernel32", use_last_error=True)

    class PE32(C.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", C.POINTER(C.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", C.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", C.c_wchar * 260)]

    snap = k32.CreateToolhelp32Snapshot(0x02, 0)
    if snap == C.c_void_p(-1).value:
        return {root_pid}
    kids = {}
    try:
        e = PE32()
        e.dwSize = C.sizeof(PE32)
        ok = k32.Process32FirstW(snap, C.byref(e))
        while ok:
            kids.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
            ok = k32.Process32NextW(snap, C.byref(e))
    finally:
        k32.CloseHandle(snap)
    out, stack = {root_pid}, [root_pid]
    while stack:
        for c in kids.get(stack.pop(), []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


# 必须声明签名。x64 下 ctypes 默认把 Python int 当 32 位传，HWND_TOPMOST(-1)
# 会变成 0x00000000FFFFFFFF 而不是全 F，Windows 认不出这个魔数——表现为窗口
# 移动生效、置顶被静默忽略。
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL


def place(hwnd):
    x, y, w, h = target_rect()
    HWND_TOPMOST, HWND_NOTOPMOST = wintypes.HWND(-1), wintypes.HWND(-2)
    SWP_SHOWWINDOW = 0x0040
    ok = user32.SetWindowPos(wintypes.HWND(hwnd),
                             HWND_TOPMOST if TOPMOST else HWND_NOTOPMOST,
                             x, y, w, h, SWP_SHOWWINDOW)
    return x, y, w, h, bool(ok)


user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                              ctypes.c_ubyte, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
# 32/64 位下取窗口样式都用 Ptr 版更稳
_get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
_set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
_get_long.restype = ctypes.c_ssize_t
_set_long.restype = ctypes.c_ssize_t
_set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]


def ensure_layered(hwnd):
    """打开 WS_EX_LAYERED，之后才能调透明度。"""
    GWL_EXSTYLE, WS_EX_LAYERED = -20, 0x00080000
    h = wintypes.HWND(hwnd)
    _set_long(h, GWL_EXSTYLE, _get_long(h, GWL_EXSTYLE) | WS_EX_LAYERED)


def set_alpha(hwnd, pct):
    """只改透明度。LWA_ALPHA 只动 alpha，不影响点击。"""
    LWA_ALPHA = 0x2
    a = max(0, min(255, int(pct * 255 / 100)))
    return bool(user32.SetLayeredWindowAttributes(wintypes.HWND(hwnd), 0, a, LWA_ALPHA))


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]


MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


try:
    _shcore = ctypes.WinDLL("shcore")
except OSError:
    _shcore = None


def dpi_for_monitor(hmon):
    """这块屏的缩放倍率（各屏可能不同）。"""
    if _shcore is not None:
        x = wintypes.UINT(); y = wintypes.UINT()
        if _shcore.GetDpiForMonitor(ctypes.c_void_p(hmon), 0,   # MDT_EFFECTIVE_DPI
                                    ctypes.byref(x), ctypes.byref(y)) == 0:
            return (x.value or 96) / 96.0
    return scale()


user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL


def mon_full_rect(hmon):
    """这块屏的【整块】矩形——放风要铺满整屏，连任务栏那条也算进去。"""
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
        m = mi.rcMonitor
        return (m.left, m.top, m.right, m.bottom)
    return None


def list_monitors():
    """返回 [(hmon, 工作区(l,t,r,b), 缩放, 是否主屏)]，工作区已挖掉任务栏。"""
    MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(_RECT), ctypes.c_double)
    out = []

    def cb(hmon, hdc, lprc, data):
        mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            w = mi.rcWork
            primary = bool(mi.dwFlags & 0x1)      # MONITORINFOF_PRIMARY
            out.append((hmon, (w.left, w.top, w.right, w.bottom), dpi_for_monitor(hmon), primary))
        return True
    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return out


def cursor_pos():
    pt = POINT()
    return pt if user32.GetCursorPos(ctypes.byref(pt)) else None


def over_window(hwnd, pt):
    r = _RECT()
    if pt and user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return r.left <= pt.x < r.right and r.top <= pt.y < r.bottom
    return False


def mon_of_point(pt):
    return user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST) if pt else None


def mon_of_window(hwnd):
    return user32.MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)


def target_alpha(hwnd, vis, ghost, pt, over):
    """按鼠标位置决定该多透明。"""
    if pt is None:
        return vis
    if over:                                    # 正指着它 → 清晰
        return vis
    same = mon_of_point(pt) == mon_of_window(hwnd)
    return ghost if same else vis               # 同屏淡出，别的屏保持清晰


def make_widget(hwnd):
    """把 Edge 的应用窗剥成无边框小部件：去标题栏、去边框、不进任务栏。"""
    GWL_STYLE, GWL_EXSTYLE = -16, -20
    WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
    WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_SYSMENU = 0x00020000, 0x00010000, 0x00080000
    WS_POPUP = 0x80000000
    WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
    h = wintypes.HWND(hwnd)

    style = _get_long(h, GWL_STYLE)
    style &= ~(WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)
    style |= WS_POPUP
    _set_long(h, GWL_STYLE, style)

    ex = _get_long(h, GWL_EXSTYLE)
    ex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW   # 工具窗 = 不在任务栏/Alt-Tab 里露脸
    _set_long(h, GWL_EXSTYLE, ex)

    # 隐藏再显示，任务栏按钮才会真的消失（光改样式不够）
    user32.ShowWindow(h, 0)      # SW_HIDE
    user32.ShowWindow(h, 5)      # SW_SHOW

    # SWP_FRAMECHANGED 让样式改动立刻生效
    SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x20
    user32.SetWindowPos(h, None, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)


_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE     # 圆角区域是 GDI 的活
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]


def round_corners(hwnd, w, h, radius_css=20):
    """给窗口切个圆角区域，做出 iPhone widget 那种圆角外形（Win10 没有系统圆角）。"""
    r = int(radius_css * scale())
    rgn = _gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r * 2, r * 2)
    user32.SetWindowRgn(wintypes.HWND(hwnd), rgn, True)   # 系统接管 rgn，不用自己删


def start_server():
    """端口已经有人占着，就当那是我们自己之前起的，直接复用。"""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", zoo_server.PORT), zoo_server.Handler)
    except OSError:
        return False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


def parse_args(argv):
    global W, H, POS, TOPMOST, ALPHA, GHOST, DYNAMIC, FOLLOW
    for i, a in enumerate(argv):
        if a == "--pos" and i + 1 < len(argv) and argv[i + 1] in ("bl", "br", "tl", "tr"):
            POS = argv[i + 1]
        elif a == "--size" and i + 1 < len(argv) and "x" in argv[i + 1]:
            try:
                W, H = (int(v) for v in argv[i + 1].lower().split("x", 1))
            except ValueError:
                pass
        elif a == "--no-top":
            TOPMOST = False
        elif a == "--opaque":
            ALPHA = 100
        elif a == "--no-ghost":
            DYNAMIC = False
        elif a == "--no-follow":
            FOLLOW = False
        elif a == "--alpha" and i + 1 < len(argv):
            try:
                ALPHA = max(40, min(100, int(argv[i + 1])))
            except ValueError:
                pass
        elif a == "--ghost" and i + 1 < len(argv):
            try:
                GHOST = max(0, min(90, int(argv[i + 1])))
            except ValueError:
                pass


def drive(hwnd, alive_fn, close_fn=None, js=None):
    """拿到原生窗口后：布置成小部件 + 跑主循环。Edge 版和 pywebview 版共用这套。
    alive_fn() 返回是否继续；close_fn() 用来收尾（Edge 是 terminate，pywebview 是 destroy）。
    js 是页面脚本桥（.run/.eval），只有 pywebview 版有——放风模式要靠它推坐标，
    Edge --app 版拿不到这个桥，那边就没有放风，其余功能一样。"""
    global _STATE, _WA, _WA_SCALE, _CORNER
    time.sleep(0.6)              # 等宿主把它自己那套初始尺寸折腾完
    make_widget(hwnd)           # 剥掉标题栏/边框、移出任务栏
    x, y, w, h, ok = place(hwnd)
    round_corners(hwnd, w, h)
    ensure_layered(hwnd)
    cur = float(ALPHA)
    set_alpha(hwnd, ALPHA)
    got_top = bool(_get_long(wintypes.HWND(hwnd), -20) & 0x8)   # WS_EX_TOPMOST
    print("   小部件已钉在 (%d, %d)  %dx%d   置顶=%s   无边框/不进任务栏" % (x, y, w, h, got_top))
    if DYNAMIC:
        if len(list_monitors()) >= 2:
            print("   透明度跟鼠标走：本屏 %d%% → 指着它/别的屏 %d%%" % (GHOST, ALPHA))
        else:
            print("   只有一块屏 → 不淡出，固定 %d%% 显示（接上第二块屏会自动恢复）" % ALPHA)
    if FOLLOW:
        print("   多屏时会躲到你鼠标不在的那块屏（宽限 %.1fs）" % GRACE)
    print("   点标题栏的 ▁ 折叠、✕ 关闭")

    last_set = ALPHA
    mons = list_monitors()
    mons_at = time.time()
    dwell = None                                  # 鼠标进入本屏、开始计时的时刻
    play = zoo_play.Playtime(hwnd, js) if js else None
    if play:
        gap = zoo_play.IDLE_ENTER
        print("   静置 %s 后放风：铺满整屏，小怪物出来自己玩，你一动就跑回来"
              % ("%.0f 秒" % gap if gap < 60 else "%.0f 分钟" % (gap / 60)))
    try:
        while alive_fn():
            want = zoo_server.UI["want"]
            if want == "close":
                break

            # —— 放风模式 ——
            # 它自己接管窗口的大小和形状，所以在这一段里要绕开下面所有的
            # 摆位/圆角/淡出逻辑，直接 continue。
            if play is not None and play.on:
                if play.step() == "done":
                    play.finish()
                    x, y, w, h, _ = place(hwnd)
                    round_corners(hwnd, w, h)
                    cur = float(ALPHA)
                    last_set = ALPHA
                    set_alpha(hwnd, ALPHA)
                    print("   ☀️ 收队，小部件归位")
                elif not play.homing and (want == "close"
                                          or zoo_play.idle_seconds() < zoo_play.IDLE_LEAVE):
                    play.begin_leave(_rect_for(_STATE))
                time.sleep(1.0 / zoo_play.FPS)
                continue
            if (play is not None
                    and zoo_play.idle_seconds() >= zoo_play.IDLE_ENTER
                    and not zoo_play.user_busy()):
                hm = mon_of_window(hwnd)
                full = mon_full_rect(hm)
                if full:
                    set_alpha(hwnd, 100)          # 放风的时候别半透明，看不清就没意思了
                    cur = last_set = 100
                    if play.enter(full, dpi_for_monitor(hm), _rect_for(_STATE)):
                        continue
                    set_alpha(hwnd, ALPHA)        # 一只都没有，原样待着
                    cur = float(ALPHA)
                    last_set = ALPHA

            if want != _STATE:
                _STATE = want
                x, y, w, h, _ = place(hwnd)
                round_corners(hwnd, w, h)         # 尺寸变了，圆角区域得重切

            pt = cursor_pos()
            over = over_window(hwnd, pt)
            now2 = time.time()

            # 隔两秒刷新一次显示器列表（应对热插拔／插投影）。这一步不能塞在
            # FOLLOW 分支里：下面判断"是不是单屏"也要用它，插拔后得跟着变。
            if now2 - mons_at > 2:
                mons = list_monitors(); mons_at = now2
            multi = len(mons) >= 2

            # —— 多屏躲避 ——
            if FOLLOW and multi and pt is not None:
                cmon = mon_of_point(pt)
                wmon = mon_of_window(hwnd)
                if cmon == wmon and not over:      # 鼠标进了它这块屏、又没指着它
                    if dwell is None:
                        dwell = now2
                    elif now2 - dwell > GRACE:     # 熬过宽限 → 溜到鼠标不在的屏
                        dest = next(((wa, ms, prim) for hm, wa, ms, prim in mons
                                     if hm != cmon), None)
                        if dest:
                            _WA, _WA_SCALE, _is_prim = dest
                            _CORNER = None                   # 每块屏都用 POS(右下角)
                            place(hwnd)                 # 先移过去（会触发跨屏 DPI 变化）
                            time.sleep(0.25)            # 等宿主折腾完 WM_DPICHANGED
                            x, y, w, h, _ = place(hwnd)  # 落定后再钉一次正确尺寸
                            round_corners(hwnd, w, h)
                        dwell = None
                else:
                    dwell = None                   # 指着它、或换屏了 → 取消计时
            else:
                dwell = None

            # —— 透明度 ——
            # 只有一块屏时不淡出：鼠标必然一直在"它那块屏"上，一淡就等于全程半隐形；
            # 而且单屏也没有别的屏可以躲，看不清纯粹是碍事。所以单屏就老老实实显示。
            if DYNAMIC and multi:
                tgt = target_alpha(hwnd, ALPHA, GHOST, pt, over)
            else:
                tgt = ALPHA
            cur += (tgt - cur) * 0.4              # 指数平滑，淡入淡出约 200ms
            if abs(cur - tgt) < 1:
                cur = tgt
            if int(round(cur)) != last_set:
                last_set = int(round(cur))
                set_alpha(hwnd, last_set)
            time.sleep(0.06)
    except KeyboardInterrupt:
        pass
    finally:
        if close_fn:
            close_fn()


def main():
    parse_args(sys.argv[1:])
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    dpi_aware()

    edge = find_edge()
    if not edge:
        print("找不到 msedge.exe。直接跑 zoo_server.py 用浏览器看吧。")
        return 1

    own = start_server()
    url = "http://127.0.0.1:%d%s" % (zoo_server.PORT, URL_SUFFIX)
    print("🐾 %s 桌面版 %s" % (BRAND, "(自带服务)" if own else "(复用已在跑的服务)"))

    x, y, w, h = target_rect()
    s = scale()
    profile = os.path.join(os.environ.get("LOCALAPPDATA", os.getcwd()), PROFILE_NAME, "edge")
    proc = subprocess.Popen([
        edge, "--app=" + url,
        "--user-data-dir=" + profile,      # 独立配置，不动你平时那个 Edge
        "--window-size=%d,%d" % (w / s, h / s),
        "--window-position=%d,%d" % (x / s, y / s),
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate,msEdgeSplitScreen",
    ])

    hwnd = find_window(proc.pid, time.time() + 20)
    if not hwnd:
        print("   窗口没抓到，位置就交给 Edge 自己了（功能不受影响）")
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        return 0

    drive(hwnd, lambda: proc.poll() is None,
          lambda: proc.terminate() if proc.poll() is None else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

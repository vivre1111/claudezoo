# -*- coding: utf-8 -*-
"""放风模式 —— 你不在的时候，把小怪物从卡片里放出来满屏跑。

3 分钟没碰键鼠就开场：窗口悄悄铺满整块屏，卡片收起来，每只怪物变成一个圆泡泡
在屏幕上溜达。你一动鼠标，它们撒腿跑回右下角，窗口缩回原样。

—— 为什么是"圆泡泡" ——
WebView2 的画面走 DirectComposition，不进窗口的重定向表面，所以 DWM 能给整窗套
alpha，却没法逐像素比色 —— 色键抠图（LWA_COLORKEY）在这儿完全无效，实测过。
唯一还管用的是窗口区域裁剪（SetWindowRgn），它在合成阶段生效，对 DComp 子内容
一样管用，而且区域外连鼠标都会穿透。代价是区域只能是硬边几何形状，所以怪物在
外面跑的时候得画成不透明的圆盘，区域取同心圆、半径比画出来的略小一点，把抗锯齿
的毛边裁掉。

—— 为什么位置由 Python 算 ——
区域和画面必须逐帧对齐，差几个像素泡泡就被切了。所以这边算好坐标，同一组数字
一份拿去建区域、一份 run_js 推给页面画，两边天然同步。实测 run_js 往返约 1ms，
25fps 一秒也就占 25ms，可以忽略。

全程只读本机状态（GetLastInputInfo / SHQueryUserNotificationState），不联网。
"""

import ctypes
import json
import math
import os
import random
import time
from ctypes import wintypes

# 默认静置 3 分钟。调试时用 ZOO_IDLE=8 之类的把门槛压下来，不用真等三分钟。
try:
    IDLE_ENTER = max(3.0, float(os.environ.get("ZOO_IDLE", "180")))
except ValueError:
    IDLE_ENTER = 180.0
IDLE_LEAVE = 2.5          # 空闲秒数掉到这个以下 = 人回来了，收队
FPS = 25.0
# 泡泡有两个半径，故意不一样：
#   BUBBLE   = 区域半径，也就是你【看得见】的那个圆
#   +SLACK   = 页面【实际画】的圆，大一整圈
# run_js 不等结果，页面把 transform 画上去比 SetWindowRgn 慢一拍。两个圆一样大的
# 话，跑起来区域就会开在页面还没画到的地方，透出窗口底色，看着像一块暗斑。多画
# 出来的这一圈永远被裁掉，不占视觉，专门吃这点延迟。
BUBBLE = 28               # 看得见的泡泡半径，CSS 像素
DISC_SLACK = 17           # 页面画的圆再往外放这么多，CSS 像素
PET_PX = 56               # 圆里那只怪物画多大，CSS 像素（要塞进 BUBBLE 里）
PAGE_API = 3              # 要求页面的 ZOO.v 至少是这个数，对不上就不放风
HOME_SPEED = 350          # 回家时的速度，CSS 像素/秒（再快就超过上面那圈余量了）
HOME_TIMEOUT = 1.6        # 跑回去最多给这么久，超了就直接收窗
RESYNC = 25.0             # 隔这么久跟页面对一次名单（状态会变，也可能来新的）

# 各状态的溜达速度（CSS 像素/秒）。在干活的精神头足，打盹的几乎不动。
SPEED = {"working": 205, "thinking": 160, "awaiting": 120, "waiting": 95,
         "dozing": 24, "stuck": 70, "quiet": 60, "broken": 60, "notranscript": 60}
# 到了目的地之后发呆多久（秒）
DWELL = {"working": (0.1, 0.5), "thinking": (0.2, 0.9), "awaiting": (0.6, 1.8),
         "waiting": (0.8, 2.4), "dozing": (4.0, 10.0)}
# 上下浮动的幅度（CSS 像素）
BOB = {"working": 5.0, "thinking": 4.0, "awaiting": 3.0, "dozing": 1.2}

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shell32 = ctypes.WinDLL("shell32")
except OSError:
    shell32 = None

# 64 位下 HRGN 是指针宽度。不声明 argtypes 的话 ctypes 按 32 位传，句柄被截断，
# CombineRgn 直接抛 OverflowError——这坑前面 SetWindowPos 的 HWND_TOPMOST 踩过一次。
gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
gdi32.CreateEllipticRgn.restype = wintypes.HANDLE
gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
gdi32.CreateRectRgn.restype = wintypes.HANDLE
gdi32.CombineRgn.argtypes = [wintypes.HANDLE] * 3 + [ctypes.c_int]
gdi32.CombineRgn.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
user32.SetWindowRgn.restype = ctypes.c_int
kernel32.GetTickCount.restype = wintypes.DWORD

RGN_OR = 2


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    """离上一次键鼠输入过了多久。纯读取，不注入任何输入。"""
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    # dwTime 是 32 位 tick，49.7 天绕一圈；拿同样 32 位的 GetTickCount 相减再截断，
    # 绕回去那一刻也不会算出个天文数字。
    delta = (kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF
    return delta / 1000.0


def user_busy():
    """全屏播放 / 投屏 / 免打扰的时候别出来蹦跶——那种时候人是在的。"""
    if shell32 is None:
        return False
    st = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(st)) != 0:
            return False
    except Exception:
        return False
    # 2=BUSY  3=全屏 D3D  4=演示模式
    return st.value in (2, 3, 4)


class Pet(object):
    __slots__ = ("id", "state", "x", "y", "tx", "ty", "wait", "phase", "vx", "vy")

    def __init__(self, pid, state, x, y):
        self.id = pid
        self.state = state
        self.x, self.y = x, y
        self.tx, self.ty = x, y
        self.wait = random.uniform(0, 0.6)
        self.phase = random.uniform(0, math.tau)
        self.vx = self.vy = 0.0

    def speed(self):
        return SPEED.get(self.state, 80)

    def bob(self, t):
        amp = BOB.get(self.state, 2.5)
        # 越活跃晃得越快
        return math.sin(t * (2.2 if self.state == "working" else 1.4) + self.phase) * amp


class Playtime(object):
    """放风的全套状态机。drive() 每轮调一次 update()，其余的它自己管。"""

    def __init__(self, hwnd, js, log=print):
        self.hwnd = hwnd
        self.js = js                  # 有 .run(code) / .eval(code) 两个方法
        self.log = log
        self.on = False               # 是否正在放风（含跑回家那一小段）
        self.homing = False           # 是否正在往回跑
        self.home_at = 0.0
        self.pets = []
        self.s = 1.0                  # 当前这块屏的缩放
        self.rect = (0, 0, 0, 0)      # 全屏窗口的物理矩形
        self.fw = self.fh = 0         # 场地大小，CSS 像素
        self.home = (0.0, 0.0)        # 回家的落点，CSS 像素（场地坐标系）
        self.t0 = 0.0
        self.last = 0.0
        self.prev = None              # 上一帧推给页面的圆心，切区域用（见 step）
        self.synced = 0.0             # 上次跟页面对名单的时刻

    # ---------- 进场 ----------

    def enter(self, mon_rect, s, widget_rect):
        """mon_rect 是整块屏的物理矩形，widget_rect 是小部件当前的物理矩形。"""
        roster = self._roster()
        if not roster:
            return False

        l, t, r, b = mon_rect
        self.rect = (l, t, r - l, b - t)
        self.s = s or 1.0
        self.fw, self.fh = (r - l) / self.s, (b - t) / self.s

        # 让它们从自己卡片所在的位置"走出来"，而不是凭空冒出来。
        # roster 给的是相对小部件窗口的 CSS 坐标，换算到全屏场地坐标系。
        wx, wy = widget_rect[0], widget_rect[1]
        self.pets = []
        for it in roster:
            px = (wx - l + (it.get("x") or 0) * self.s) / self.s
            py = (wy - t + (it.get("y") or 0) * self.s) / self.s
            px = min(max(px, BUBBLE), self.fw - BUBBLE)
            py = min(max(py, BUBBLE), self.fh - BUBBLE)
            self.pets.append(Pet(it["id"], it.get("state") or "waiting", px, py))

        # 顺序很重要：先让页面切到放风布局并摆好初始位置，再放大窗口，
        # 最后才上区域。反过来的话中间会闪一帧"全屏大卡片"。
        self.js.run("window.ZOO && ZOO.playOn(%s, %d, %d)"
                    % (json.dumps(roster, ensure_ascii=False),
                       (BUBBLE + DISC_SLACK) * 2, PET_PX))
        # NOACTIVATE 很重要：你人不在，这窗口没道理把焦点抢走——不然回来一看，
        # 刚才在敲的那个编辑器失焦了。
        SWP_SHOWWINDOW, SWP_NOACTIVATE = 0x0040, 0x0010
        user32.SetWindowPos(wintypes.HWND(self.hwnd), wintypes.HWND(-1),
                            l, t, r - l, b - t, SWP_SHOWWINDOW | SWP_NOACTIVATE)
        self.on, self.homing = True, False
        self.t0 = self.last = self.synced = time.time()
        self.prev = None
        self._region(self._push())
        self.log("   🌙 放风开始：%d 只出栏，场地 %.0fx%.0f" % (len(self.pets), self.fw, self.fh))
        return True

    # ---------- 每帧 ----------

    def step(self):
        now = time.time()
        dt = min(0.12, max(0.001, now - self.last))     # 卡顿时别让它们瞬移
        self.last = now
        t = now - self.t0

        # 你可能一走就是几个小时，名单不能停在进场那一刻：那样状态永远是旧的，
        # 新开的 session 也进不来。隔一会儿对一次表。
        if not self.homing and now - self.synced > RESYNC:
            self.synced = now
            self._resync()

        for p in self.pets:
            if self.homing:
                p.tx, p.ty = self.home
                spd = HOME_SPEED
            else:
                spd = p.speed()
                if p.wait > 0:
                    p.wait -= dt
                else:
                    dx, dy = p.tx - p.x, p.ty - p.y
                    if dx * dx + dy * dy < 9:
                        self._retarget(p)
            if p.wait <= 0 or self.homing:
                dx, dy = p.tx - p.x, p.ty - p.y
                d = math.hypot(dx, dy)
                if d > 0.5:
                    step = min(d, spd * dt)
                    p.vx, p.vy = dx / d * step, dy / d * step
                    p.x += p.vx
                    p.y += p.vy
                else:
                    p.vx = p.vy = 0.0

        if not self.homing:
            self._separate()

        # 先推坐标，再拿【上一帧】的坐标去切区域。
        # run_js 不等结果，页面把 transform 画上去比这边慢一拍；区域要是按当前帧
        # 立刻切，跑得快的时候两边能差出十几个物理像素，泡泡边上就被削掉一块。
        # 让区域同样晚一帧，两边的延迟就抵消了。
        frame = self._push(t)
        self._region(self.prev or frame)
        self.prev = frame

        if self.homing and (now - self.home_at > HOME_TIMEOUT or self._all_home()):
            return "done"
        return "on"

    def _resync(self):
        """重新对一次名单：状态变了就跟着变，来了新的就从边上跑进来，走了的就撤掉。
        活着的按 id 复用，位置保留——不然每次对表所有小怪物都会瞬移。"""
        roster = self._roster()
        if not roster:
            return                      # 名单空了/页面没响应，保持现状，别把场子清了
        old = {p.id: p for p in self.pets}
        pets = []
        for it in roster:
            p = old.get(it["id"])
            if p is None:               # 新来的：从随机一条边溜进来
                x, y = self._edge_spot()
                p = Pet(it["id"], it.get("state") or "waiting", x, y)
            else:
                p.state = it.get("state") or p.state
            pets.append(p)
        self.pets = pets
        self.prev = None                # 顺序可能变了，上一帧的坐标对不上号
        self.js.run("window.ZOO && ZOO.playSync(%s)"
                    % json.dumps(roster, ensure_ascii=False))

    def _edge_spot(self):
        side = random.randrange(4)
        if side == 0:
            return random.uniform(BUBBLE, self.fw - BUBBLE), BUBBLE
        if side == 1:
            return random.uniform(BUBBLE, self.fw - BUBBLE), self.fh - BUBBLE
        if side == 2:
            return BUBBLE, random.uniform(BUBBLE, self.fh - BUBBLE)
        return self.fw - BUBBLE, random.uniform(BUBBLE, self.fh - BUBBLE)

    def _retarget(self, p):
        lo, hi = DWELL.get(p.state, (0.6, 2.0))
        p.wait = random.uniform(lo, hi)
        m = BUBBLE + 6
        if p.state == "dozing":
            # 打盹的只挪一小步，而且爱往边上靠
            p.tx = min(max(p.x + random.uniform(-160, 160), m), self.fw - m)
            p.ty = min(max(p.y + random.uniform(-90, 90), m), self.fh - m)
        else:
            p.tx = random.uniform(m, self.fw - m)
            p.ty = random.uniform(m, self.fh - m)

    def _separate(self):
        """别挤成一坨。只有十来只，两两比一遍不心疼。"""
        n = len(self.pets)
        gap = BUBBLE * 2.05
        for i in range(n):
            a = self.pets[i]
            for j in range(i + 1, n):
                b = self.pets[j]
                dx, dy = b.x - a.x, b.y - a.y
                d2 = dx * dx + dy * dy
                if 0 < d2 < gap * gap:
                    d = math.sqrt(d2)
                    push = (gap - d) * 0.5
                    ux, uy = dx / d, dy / d
                    a.x -= ux * push
                    a.y -= uy * push
                    b.x += ux * push
                    b.y += uy * push
        for p in self.pets:
            p.x = min(max(p.x, BUBBLE), self.fw - BUBBLE)
            p.y = min(max(p.y, BUBBLE), self.fh - BUBBLE)

    def _push(self, t=0.0):
        """位置推给页面，并把这一帧的圆心返回去（切区域要用同一组数）。
        扁平数组：每只三个数 x, y, 歪头角度。"""
        flat, centers = [], []
        for p in self.pets:
            y = p.y + p.bob(t)
            centers.append((p.x, y))
            flat.append(round(p.x, 1))
            flat.append(round(y, 1))
            flat.append(round(max(-16.0, min(16.0, p.vx * 2.2)), 1))
        self.js.run("window.ZOO && ZOO.playAt(%s)" % json.dumps(flat))
        return centers

    def _region(self, centers):
        """重切窗口区域——只有泡泡那几个圆是"实"的，其余全透明且鼠标穿透。"""
        r = max(4, int(BUBBLE * self.s))
        rgn = gdi32.CreateRectRgn(0, 0, 0, 0)
        for (x, y) in centers:
            cx, cy = int(x * self.s), int(y * self.s)
            e = gdi32.CreateEllipticRgn(cx - r, cy - r, cx + r, cy + r)
            gdi32.CombineRgn(rgn, rgn, e, RGN_OR)
            gdi32.DeleteObject(e)
        # 系统接管 rgn，不用也不能自己删
        user32.SetWindowRgn(wintypes.HWND(self.hwnd), rgn, True)

    # ---------- 收队 ----------

    def begin_leave(self, widget_rect):
        """人回来了：定个家的位置，全体加速跑回去。"""
        if self.homing:
            return
        l, t = self.rect[0], self.rect[1]
        wx, wy, ww, wh = widget_rect
        self.home = ((wx - l + ww * 0.5) / self.s, (wy - t + wh * 0.85) / self.s)
        self.homing = True
        self.home_at = time.time()
        self.js.run("window.ZOO && ZOO.playHome()")

    def _all_home(self):
        return all(math.hypot(p.x - self.home[0], p.y - self.home[1]) < 26 for p in self.pets)

    def finish(self):
        """窗口交还给 drive()——它随后会 place() + round_corners() 复原。"""
        self.on = self.homing = False
        self.pets = []
        self.js.run("window.ZOO && ZOO.playOff()")

    # ---------- 内部 ----------

    def _roster(self):
        # 认版本号：窗口开着的时候换了 zoo.html，页面还是旧的，画不出泡泡；
        # 而区域照切不误，屏幕上就会出现一堆会跑的黑圆。宁可这次不放风。
        try:
            r = self.js.eval("(window.ZOO && ZOO.v >= %d) ? ZOO.roster() : []" % PAGE_API)
        except Exception as e:
            self.log("   放风取名单失败：%s" % e)
            return []
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except ValueError:
                return []
        return [it for it in (r or []) if isinstance(it, dict) and it.get("id")]

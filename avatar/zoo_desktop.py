# -*- coding: utf-8 -*-
"""
ClaudeZoo 桌面版（pywebview）—— 真正的独立桌面 app，不走 Edge 浏览器窗口。

进程是它自己（python / 打包后是 .exe），窗口是原生窗口，任务栏里没有 Edge。
底层渲染用系统自带的 WebView2 运行时（一个系统组件，不是"打开浏览器"）。
无边框、半透明、圆角、跟随鼠标躲屏、折叠——全都在，复用 zoo_app 里的那套 Win32 逻辑。

    python zoo_desktop.py            # 参数跟 zoo_app 一样：--alpha/--ghost/--pos/--no-follow …
"""

import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

# pywebview 现在装在 D:\avatar\.venv 里，任务计划直接用这个 venv 的 pythonw.exe 跑，
# 不用再手动把系统 Python 的 site-packages 塞进 sys.path 了——以前这么干是因为当时
# 只有系统 Python 装了 pywebview；但留着不删的话，系统那份会抢在 venv 自己的前面
# 被 import 到，pywebview 内部一检测环境不对就会另开一个系统 Python 子进程重跑整个
# 脚本，多一层跨解释器自我重启，正是开机有时候起不来的病根。
import webview

import zoo_app as za
import zoo_server


def start_server():
    """自带数据服务（只读本机文件）。端口被占就复用。"""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", zoo_server.PORT), zoo_server.Handler)
    except OSError:
        return False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


def worker(window):
    """webview 起来后在后台线程里跑：抓到原生窗口 → 布置 → 主循环。"""
    # 这个窗口就在本进程里，按标题在本进程的窗口中找它
    hwnd = za.find_window(os.getpid(), time.time() + 15)
    if not hwnd:
        print("   没抓到原生窗口")
        return

    # 确认页面真的用 WebView2 渲染出来了（不是白屏、不是回退到 IE）
    for _ in range(24):
        try:
            n = window.evaluate_js("document.querySelectorAll('.pen').length")
        except Exception:
            n = None
        if n:
            bf = window.evaluate_js("CSS.supports('backdrop-filter','blur(2px)') || "
                                    "CSS.supports('-webkit-backdrop-filter','blur(2px)')")
            print("   页面已渲染：%s 张卡片，毛玻璃支持=%s" % (n, bf))
            break
        time.sleep(0.5)

    alive = {"v": True}
    window.events.closed += lambda: alive.__setitem__("v", False)

    def close():
        try:
            window.destroy()
        except Exception:
            pass
        alive["v"] = False

    za.drive(hwnd, lambda: alive["v"], close, _JS(window))


class _JS(object):
    """给放风模式用的脚本桥。run 不等结果（每帧推坐标，等一下都嫌慢），
    eval 要结果（取出栏名单）。窗口关掉之后这些调用会抛，一律吞掉。"""

    def __init__(self, window):
        self.w = window

    def run(self, code):
        try:
            self.w.run_js(code)
        except Exception:
            pass

    def eval(self, code):
        return self.w.evaluate_js(code)


def main():
    try:
        return _run()
    except Exception:
        import traceback
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claudezoo_error.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            traceback.print_exc(file=f)
        raise


def _run():
    za.parse_args(sys.argv[1:])

    # pythonw.exe 没有控制台，所有 print() 平时都是直接扔进黑洞——包括 worker()/
    # za.drive() 里"没抓到原生窗口"这类关键排错信息。开机自启这种没人盯着屏幕的
    # 场景，出问题了完全没法诊断。这里把 stdout/stderr 接到一个日志文件上，以后
    # 想查某次开机到底起没起来、卡在哪一步，直接看 claudezoo.log 就行。
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claudezoo.log")
    log_f = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = log_f
    print("\n=== %s  启动 (pid=%d) ===" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # DPI 感知要在建窗口之前设，坐标算的才是物理像素
    za.dpi_aware()

    # 单实例：端口被占 = 已经有一个在跑，别再开第二个窗口
    if not start_server():
        print("ClaudeZoo 好像已经在跑了（端口 %d 被占），本次不重复启动。" % zoo_server.PORT)
        return 0
    url = "http://127.0.0.1:%d" % zoo_server.PORT
    print("🐾 ClaudeZoo 桌面版 (pywebview, 独立 app)")

    x, y, w, h = za.target_rect()
    s = za.scale()
    window = webview.create_window(
        za.TITLE_HINT, url=url,
        frameless=True, easy_drag=False, on_top=za.TOPMOST,
        width=int(w / s), height=int(h / s),        # pywebview 用 DIP，先按缩放折算
        x=int(x / s), y=int(y / s),
        background_color="#0E1116",
    )
    # 必须强制 edgechromium(WebView2)。不指定的话 pywebview 会挑到老 IE(MSHTML)，
    # 毛玻璃/网格动画/color-mix 全画不出来。WebView2 运行时是系统组件，机器上已有。
    store = os.path.join(os.environ.get("LOCALAPPDATA", os.getcwd()), "ClaudeZoo", "webview")
    # webview.start 会阻塞在主线程跑 GUI；worker 在它 spawn 的线程里跑
    webview.start(worker, window, gui="edgechromium",
                  private_mode=False, storage_path=store)
    print("=== %s  窗口关了，退出 ===" % time.strftime("%Y-%m-%d %H:%M:%S"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

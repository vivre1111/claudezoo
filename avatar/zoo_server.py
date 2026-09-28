# -*- coding: utf-8 -*-
"""
Claude Zoo — 把本机所有活着的 Claude Code session 变成一屋子小动物。

只读。不碰任何 session 的进程，不写 ~/.claude 里的任何东西。

    python zoo_server.py          # 然后打开 http://127.0.0.1:8777
"""

import ctypes
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
CURSOR_ROOT = os.path.join(os.environ.get("APPDATA", ""), "Cursor", "User")
CURSOR_DB = os.path.join(CURSOR_ROOT, "globalStorage", "state.vscdb")
CURSOR_WS = os.path.join(CURSOR_ROOT, "workspaceStorage")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8777

# 会话管理小工具（另一个项目：只读地列出 ~/.claude 下的会话，并生成删除命令；
# 它自己不删任何东西）。这里有两条路径写死了本机位置——它是给自己用的本地
# 工具，不打算装到别的机器上。
# 名字必须带 SESS_TOOL_ 前缀：上面第 26 行的 SESSIONS_DIR 是本文件自己的常量
# （~/.claude/sessions），早先这里直接用 SESSIONS_DIR 命名，把它覆盖掉了 ——
# collect() 于是跑去 D:\claude-session-remove-app 找会话注册文件，满屋一只不剩。
SESS_TOOL_APP = r"D:\claude-session-remove-app\app.js"
SESS_TOOL_DIR = r"D:\claude-session-remove-app"
SESS_TOOL_URL = "http://127.0.0.1:8756/"
NODE_EXE = r"C:\Users\vivre\Desktop\workspace\tools\node\node.exe"

TAIL_BYTES = 300_000      # 转录文件从尾部读多少
LIVE_WINDOW = 90          # 秒内有写入 = 还在动（现在只用来判断「小弟还在不在跑」）
DOZE_AFTER = 300          # 静置这么久（5 分钟）才闭眼睡；在那之前一直举着手等你
CONTEXT_MAX = 1_000_000   # 上下文仪表盘满格
CODA_MAX_HELPERS = 20     # codex 小弟计数上限，防止异常值把“有几个小弟”拉爆
MAX_IDLE_SEC = 24 * 3600  # 静置超过这么久(24 小时)就不显示了。Cursor 不会自动关掉
                          # 闲置对话，会攒一大堆老 task，靠这条门槛滤掉

# 固定的项目：这些会永远置顶显示，卡片上多两个快捷按钮（开文件夹 / 浏览器打开），
# 而且不受 24 小时静置过滤。cwd 用来跟正在跑的 session 对上号；对不上就显示一张
# 占位卡，按钮照样能用（相当于一个小启动器）。
PINS = []                               # 置顶全部取消——只显示真正在跑的会话。
# 以前钉着的四个，想恢复就取消下面的注释、塞回上面的 PINS 里：
# {"name": "电机在线监测", "cwd": r"C:\Users\vivre\Desktop\workspace\电机在线监测",
#  "url": "http://192.168.1.95:8301/"},
# {"name": "无人值守",     "cwd": r"C:\Users\vivre\Desktop\workspace\unattended",
#  "url": "http://localhost:5200/#/index"},
# {"name": "知识库",       "cwd": r"D:\rag",
#  "url": "http://192.168.1.95:8000"},
# {"name": "工厂表计识别",  "cwd": r"D:\analog_reading_v2",
#  "url": ""},



# ---------------------------------------------------------------- 深夜片场
# 钉在列表最底部的一张海报卡，不算会话、不进 24 小时过滤、不参与排序。
# 海报本身是纯 CSS 画的色块，不抓真实剧照——台词也只留一两句短的，标好片名年份。
# 按「今天的日期」选片，同一天刷新多少次都是这张，第二天自动换下一部。
MOVIES = [
    {"title": "绿野仙踪", "en": "The Wizard of Oz", "year": 1939, "hue": 42,
     "quote": "There's no place like home."},
    {"title": "卡萨布兰卡", "en": "Casablanca", "year": 1942, "hue": 205,
     "quote": "Here's looking at you, kid."},
    {"title": "罗生门", "en": "Rashomon", "year": 1950, "hue": 75,
     "quote": "It's human to lie. Most of the time we can't even be honest with ourselves."},
    {"title": "东京物语", "en": "Tokyo Story", "year": 1953, "hue": 168,
     "quote": "Isn't life disappointing?"},
    {"title": "罗马假日", "en": "Roman Holiday", "year": 1953, "hue": 20,
     "quote": "Rome. By all means, Rome."},
    {"title": "七武士", "en": "Seven Samurai", "year": 1954, "hue": 90,
     "quote": "The farmers have won. We have lost."},
    {"title": "十二怒汉", "en": "12 Angry Men", "year": 1957, "hue": 45,
     "quote": "Prejudice always obscures the truth."},
    {"title": "公寓春光", "en": "The Apartment", "year": 1960, "hue": 30,
     "quote": "Shut up and deal."},
    {"title": "杀死一只知更鸟", "en": "To Kill a Mockingbird", "year": 1962, "hue": 55,
     "quote": "You never really understand a person until you climb into his skin."},
    {"title": "音乐之声", "en": "The Sound of Music", "year": 1965, "hue": 100,
     "quote": "When the Lord closes a door, somewhere He opens a window."},
    {"title": "2001太空漫游", "en": "2001: A Space Odyssey", "year": 1968, "hue": 215,
     "quote": "I'm sorry, Dave. I'm afraid I can't do that."},
    {"title": "教父", "en": "The Godfather", "year": 1972, "hue": 15,
     "quote": "I'm gonna make him an offer he can't refuse."},
    {"title": "飞越疯人院", "en": "One Flew Over the Cuckoo's Nest", "year": 1975, "hue": 175,
     "quote": "But I tried, didn't I? At least I did that."},
    {"title": "洛奇", "en": "Rocky", "year": 1976, "hue": 10,
     "quote": "All I wanna do is go the distance."},
    {"title": "星球大战", "en": "Star Wars", "year": 1977, "hue": 240,
     "quote": "May the Force be with you."},
    {"title": "异形", "en": "Alien", "year": 1979, "hue": 120,
     "quote": "In space, no one can hear you scream."},
    {"title": "帝国反击战", "en": "The Empire Strikes Back", "year": 1980, "hue": 250,
     "quote": "Do. Or do not. There is no try."},
    {"title": "E.T. 外星人", "en": "E.T. the Extra-Terrestrial", "year": 1982, "hue": 300,
     "quote": "E.T. phone home."},
    {"title": "银翼杀手", "en": "Blade Runner", "year": 1982, "hue": 260,
     "quote": "All those moments will be lost in time, like tears in rain."},
    {"title": "回到未来", "en": "Back to the Future", "year": 1985, "hue": 40,
     "quote": "Roads? Where we're going, we don't need roads."},
    {"title": "龙猫", "en": "My Neighbor Totoro", "year": 1988, "hue": 95,
     "quote": "Trees and people used to be good friends."},
    {"title": "天堂电影院", "en": "Cinema Paradiso", "year": 1988, "hue": 25,
     "quote": "Life isn't like in the movies. Life is much harder."},
    {"title": "魔女宅急便", "en": "Kiki's Delivery Service", "year": 1989, "hue": 335,
     "quote": "We each need to find our own inspiration."},
    {"title": "死亡诗社", "en": "Dead Poets Society", "year": 1989, "hue": 145,
     "quote": "Carpe diem. Seize the day, boys."},
    {"title": "侏罗纪公园", "en": "Jurassic Park", "year": 1993, "hue": 110,
     "quote": "Life finds a way."},
    {"title": "辛德勒的名单", "en": "Schindler's List", "year": 1993, "hue": 0,
     "quote": "Whoever saves one life, saves the world entire."},
    {"title": "霸王别姬", "en": "Farewell My Concubine", "year": 1993, "hue": 358,
     "quote": "差一年，一个月，一天，一个时辰，都不算一辈子。"},
    {"title": "肖申克的救赎", "en": "The Shawshank Redemption", "year": 1994, "hue": 28,
     "quote": "Get busy living, or get busy dying."},
    {"title": "阿甘正传", "en": "Forrest Gump", "year": 1994, "hue": 130,
     "quote": "Life is like a box of chocolates."},
    {"title": "这个杀手不太冷", "en": "Léon: The Professional", "year": 1994, "hue": 165,
     "quote": "Is life always this hard, or is it just when you're a kid?"},
    {"title": "狮子王", "en": "The Lion King", "year": 1994, "hue": 35,
     "quote": "Remember who you are."},
    {"title": "重庆森林", "en": "Chungking Express", "year": 1994, "hue": 172,
     "quote": "不知道从什么时候开始，在每个东西上面都有个日期。"},
    {"title": "低俗小说", "en": "Pulp Fiction", "year": 1994, "hue": 50,
     "quote": "That's a pretty good milkshake."},
    {"title": "饮食男女", "en": "Eat Drink Man Woman", "year": 1994, "hue": 18,
     "quote": "人生不能像做菜，把所有的料都准备好了才下锅。"},
    {"title": "阿波罗13号", "en": "Apollo 13", "year": 1995, "hue": 212,
     "quote": "Houston, we have a problem."},
    {"title": "玩具总动员", "en": "Toy Story", "year": 1995, "hue": 210,
     "quote": "To infinity and beyond!"},
    {"title": "大话西游之大圣娶亲", "en": "A Chinese Odyssey", "year": 1995, "hue": 320,
     "quote": "曾经有一份真挚的爱情摆在我面前。"},
    {"title": "泰坦尼克号", "en": "Titanic", "year": 1997, "hue": 195,
     "quote": "I'm the king of the world!"},
    {"title": "美丽人生", "en": "Life Is Beautiful", "year": 1997, "hue": 60,
     "quote": "This is my story. This is the sacrifice my father made."},
    {"title": "千钧一发", "en": "Gattaca", "year": 1997, "hue": 158,
     "quote": "I never saved anything for the swim back."},
    {"title": "楚门的世界", "en": "The Truman Show", "year": 1998, "hue": 185,
     "quote": "In case I don't see you: good afternoon, good evening, and good night."},
    {"title": "海上钢琴师", "en": "The Legend of 1900", "year": 1998, "hue": 220,
     "quote": "You're never really done for, as long as you've got a good story."},
    {"title": "黑客帝国", "en": "The Matrix", "year": 1999, "hue": 135,
     "quote": "There is no spoon."},
    {"title": "绿里奇迹", "en": "The Green Mile", "year": 1999, "hue": 88,
     "quote": "I'm tired, boss."},
    {"title": "花样年华", "en": "In the Mood for Love", "year": 2000, "hue": 355,
     "quote": "那些消逝了的岁月，仿佛隔着一块积着灰尘的玻璃。"},
    {"title": "卧虎藏龙", "en": "Crouching Tiger, Hidden Dragon", "year": 2000, "hue": 150,
     "quote": "把手握紧，里面什么也没有；把手松开，你拥有的是一切。"},
    {"title": "一一", "en": "Yi Yi", "year": 2000, "hue": 142,
     "quote": "你自己看不到，所以我拍给你看。"},
    {"title": "千与千寻", "en": "Spirited Away", "year": 2001, "hue": 275,
     "quote": "You never really forget someone once you've met them."},
    {"title": "怪兽电力公司", "en": "Monsters, Inc.", "year": 2001, "hue": 190,
     "quote": "We scare because we care."},
    {"title": "天使爱美丽", "en": "Amélie", "year": 2001, "hue": 345,
     "quote": "Times are hard for dreamers."},
    {"title": "指环王：护戒使者", "en": "The Fellowship of the Ring", "year": 2001, "hue": 105,
     "quote": "All we have to decide is what to do with the time that is given us."},
    {"title": "无间道", "en": "Infernal Affairs", "year": 2002, "hue": 200,
     "quote": "对不起，我是警察。"},
    {"title": "大鱼", "en": "Big Fish", "year": 2003, "hue": 155,
     "quote": "A man tells his stories so many times that he becomes the stories."},
    {"title": "海底总动员", "en": "Finding Nemo", "year": 2003, "hue": 198,
     "quote": "Just keep swimming."},
    {"title": "加勒比海盗", "en": "Pirates of the Caribbean", "year": 2003, "hue": 22,
     "quote": "But why is the rum gone?"},
    {"title": "超人总动员", "en": "The Incredibles", "year": 2004, "hue": 340,
     "quote": "Everyone's special, which is another way of saying no one is."},
    {"title": "当幸福来敲门", "en": "The Pursuit of Happyness", "year": 2006, "hue": 48,
     "quote": "Don't ever let somebody tell you you can't do something."},
    {"title": "料理鼠王", "en": "Ratatouille", "year": 2007, "hue": 20,
     "quote": "Anyone can cook."},
    {"title": "机器人总动员", "en": "WALL·E", "year": 2008, "hue": 30,
     "quote": "Define dancing."},
    {"title": "蝙蝠侠：黑暗骑士", "en": "The Dark Knight", "year": 2008, "hue": 235,
     "quote": "Why so serious?"},
    {"title": "飞屋环游记", "en": "Up", "year": 2009, "hue": 350,
     "quote": "Adventure is out there!"},
    {"title": "三傻大闹宝莱坞", "en": "3 Idiots", "year": 2009, "hue": 65,
     "quote": "All is well."},
    {"title": "阿凡达", "en": "Avatar", "year": 2009, "hue": 178,
     "quote": "I see you."},
    {"title": "盗梦空间", "en": "Inception", "year": 2010, "hue": 245,
     "quote": "You mustn't be afraid to dream a little bigger, darling."},
    {"title": "让子弹飞", "en": "Let the Bullets Fly", "year": 2010, "hue": 12,
     "quote": "站着，还把钱挣了。"},
    {"title": "触不可及", "en": "The Intouchables", "year": 2011, "hue": 82,
     "quote": "No arms, no chocolate."},
    {"title": "少年派的奇幻漂流", "en": "Life of Pi", "year": 2012, "hue": 180,
     "quote": "The whole of life becomes an act of letting go."},
    {"title": "星际穿越", "en": "Interstellar", "year": 2014, "hue": 230,
     "quote": "Love is the one thing that transcends time and space."},
    {"title": "爆裂鼓手", "en": "Whiplash", "year": 2014, "hue": 8,
     "quote": "There are no two words more harmful than 'good job'."},
    {"title": "银河护卫队", "en": "Guardians of the Galaxy", "year": 2014, "hue": 265,
     "quote": "We are Groot."},
    {"title": "布达佩斯大饭店", "en": "The Grand Budapest Hotel", "year": 2014, "hue": 330,
     "quote": "There are still faint glimmers of civilization left in this barbaric slaughterhouse."},
    {"title": "疯狂动物城", "en": "Zootopia", "year": 2016, "hue": 115,
     "quote": "Anyone can be anything."},
    {"title": "你的名字。", "en": "Your Name", "year": 2016, "hue": 285,
     "quote": "I'm always searching for something, for someone."},
    {"title": "降临", "en": "Arrival", "year": 2016, "hue": 170,
     "quote": "If you could see your whole life, would you change things?"},
    {"title": "寻梦环游记", "en": "Coco", "year": 2017, "hue": 305,
     "quote": "The real death is when no one in the world remembers you."},
    {"title": "三块广告牌", "en": "Three Billboards Outside Ebbing, Missouri", "year": 2017, "hue": 5,
     "quote": "Anger begets greater anger."},
    {"title": "我不是药神", "en": "Dying to Survive", "year": 2018, "hue": 68,
     "quote": "他才二十岁，他只是想活着，他有什么罪？"},
    {"title": "蜘蛛侠：平行宇宙", "en": "Into the Spider-Verse", "year": 2018, "hue": 315,
     "quote": "Anyone can wear the mask."},
    {"title": "流浪地球", "en": "The Wandering Earth", "year": 2019, "hue": 208,
     "quote": "希望是我们唯一回家的路。"},
    {"title": "哪吒之魔童降世", "en": "Ne Zha", "year": 2019, "hue": 8,
     "quote": "我命由我不由天。"},
    {"title": "心灵奇旅", "en": "Soul", "year": 2020, "hue": 225,
     "quote": "I'm going to live every minute of it."},
    {"title": "沙丘", "en": "Dune", "year": 2021, "hue": 35,
     "quote": "Fear is the mind-killer."},
    {"title": "瞬息全宇宙", "en": "Everything Everywhere All at Once", "year": 2022, "hue": 290,
     "quote": "Be kind, especially when we don't know what's going on."},
]


def _walk_step(n):
    """挑一个跟片单长度互质的步长，用来在片单上「跳着走」。

    互质保证走满 n 天正好把 n 部片放一遍：一部不落，也一部不重。
    步长取黄金分割（0.618n）附近的那个互质数：正好一半的话相邻两天会在单子上
    来回弹（8→49→7→48…），编号一眼就看出规律；黄金分割不会落进这种小周期，
    撒得最匀。以后往片单里加片子不用改这儿，它跟着长度自己算。
    """
    import math
    if n < 3:
        return 1
    want = max(2, int(n * 0.618))
    for off in range(n):                     # 从黄金分割点往两边找最近的互质数
        for k in (want - off, want + off):
            if 2 <= k < n and math.gcd(k, n) == 1:
                return k
    return 1


def pick_movie(now):
    import datetime
    d = datetime.date.fromtimestamp(now)
    # 早先是拿日期字符串的字符和取模。片子只有 12 部的时候正好够用，但那个和
    # 一年到头只能取到几十个值，片单一长，后半张单子就永远抽不到了。改成按
    # 「第几天」跳着走：每天必换，走满一整轮才回头。
    n = len(MOVIES)
    idx = (d.toordinal() * _walk_step(n)) % n
    m = dict(MOVIES[idx])
    m["day"] = d.isoformat()
    m["no"] = idx + 1
    m["count"] = n
    return m


def _norm(p):
    return os.path.normcase(os.path.normpath(p)) if p else ""


def apply_pins(cards):
    """pin 认「项目名」：会话标题 == pin 名字就算命中（最优先）。标题对不上时
    退回按文件夹匹配（越深的子目录越具体），这样自动生成的英文标题、或项目临时
    开在别处的会话也不会漏。每个 PIN 命中多个会话时挑最近动过的那个（lastTs 最新，
    刚开没说话的排最后）；一个都没有就给张占位卡。其余会话照常当普通卡显示。"""
    paths = [_norm(p["cwd"]) for p in PINS]

    claim = {}                          # pin 下标 -> 命中它的卡片列表
    for c in cards:
        cn = _norm(c.get("cwd"))
        title = (c.get("title") or "").strip().casefold()
        best_i, best_score = None, 0
        for i, p in enumerate(PINS):
            if title and title == p["name"].strip().casefold():
                score = 1_000_000                          # 项目名对上，压倒一切
            elif paths[i] and (cn == paths[i] or cn.startswith(paths[i] + os.sep)):
                score = len(paths[i])                      # 退回文件夹匹配，取最深的
            else:
                continue
            if score > best_score:
                best_i, best_score = i, score
        if best_i is not None:
            claim.setdefault(best_i, []).append(c)

    placeholders = []
    for i in range(len(PINS)):
        matches = claim.get(i)
        if not matches:
            placeholders.append(pin_placeholder(i, PINS[i]))
            continue
        best = max(matches, key=lambda c: (c.get("lastTs") or 0, c.get("name") or ""))
        best["pin"] = i
        best["pinName"] = PINS[i]["name"]
        best["pinUrl"] = PINS[i].get("url", "")
    return placeholders


def pin_placeholder(i, p):
    """PIN 当前没有在跑的会话时，给它一张只带快捷按钮的占位卡。"""
    return {
        "id": "pin:%d" % i, "kind": "claude", "pid": None,
        "name": p["name"], "title": "", "cwd": p["cwd"],
        "folder": os.path.basename(p["cwd"].rstrip("\\/")) or p["cwd"],
        "version": "", "entrypoint": "", "startedAt": None,
        "state": "quiet", "emoji": "📌", "verb": "已固定 · 没有在跑的会话",
        "hint": "", "say": "", "ask": "", "done": False, "model": "", "branch": "",
        "context": 0, "ctxPct": 0, "ctxLabel": "", "idle": None,
        "sizeMB": 0, "helpers": 0, "beats": [0] * 12, "lastTs": None, "sayTs": None,
        "pin": i, "pinName": p["name"], "pinUrl": p.get("url", ""),
    }


# 卡片 id -> {"cwd":…, "url":…}。每次 collect() 重建，/api/open 只认这张表里的 id。
# 这样按钮能给每张卡都配上，同时这个本地端点仍然开不了任意路径——URL 里传的是
# 卡片 id，不是路径，想开什么完全由后端这张表说了算。
OPENABLE = {}


def open_card(cid, what):
    """点卡片按钮 → 打开这张卡对应的文件夹或网址。全是本机动作，无对外请求。"""
    ent = OPENABLE.get(cid)
    if not ent:
        return False
    try:
        if what == "folder" and ent.get("cwd"):
            if hasattr(os, "startfile"):
                os.startfile(ent["cwd"])               # 资源管理器打开文件夹
            else:
                webbrowser.open("file:///" + ent["cwd"].replace("\\", "/"))
            return True
        if what == "url" and ent.get("url"):
            webbrowser.open(ent["url"])                 # 默认浏览器打开
            return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- 会话管理入口
# 顶栏那个 🗂 按钮走这里：把隔壁的"会话管理"小工具叫起来，再交给默认浏览器。

def _sessions_alive(timeout=0.5):
    """隔壁服务在不在。纯本机回环探测，不对外。"""
    try:
        with urllib.request.urlopen(SESS_TOOL_URL, timeout=timeout):
            return True
    except Exception:
        return False


def launch_sessions():
    """确保会话管理服务在跑，然后用默认浏览器打开它。

    它平时由计划任务在登录后两分钟就起来了，所以多数时候这里只做第一行。
    万一没起（被关掉了、或者还没到点），就地拉一个无窗口的起来——拉起来
    之后还要轮询等它就绪：它启动时要先扫一遍 ~/.claude，冷启动一两秒，
    直接 open 会给浏览器一个打不开的页面。
    """
    if not _sessions_alive():
        if not os.path.exists(SESS_TOOL_APP):
            return False
        node = NODE_EXE if os.path.exists(NODE_EXE) else "node"
        try:
            subprocess.Popen(
                [node, SESS_TOOL_APP],
                cwd=SESS_TOOL_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return False

        deadline = time.time() + 8
        while time.time() < deadline:
            if _sessions_alive():
                break
            time.sleep(0.2)

    webbrowser.open(SESS_TOOL_URL)
    return True


# ---------------------------------------------------------------- 进程探活
# 注意：绝对不能用 os.kill(pid, 0) —— 在 Windows 上 Python 会把它翻译成
# TerminateProcess，真的会把人家 session 杀掉。这里用只读的 Win32 调用。

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    def process_info(pid):
        """返回 (是否存活, 映像名)。任何一步失败都当作已死。"""
        h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False, ""
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False, ""
            if code.value != STILL_ACTIVE:
                return False, ""
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            name = ""
            if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
            return True, name
        finally:
            _k32.CloseHandle(h)

    TH32CS_SNAPPROCESS = 0x02
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    def count_processes(exe_lower):
        """数一下有几个同名进程。Cursor 是 Electron，会有一大把。"""
        snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE:
            return 0
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            n = 0
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                if e.szExeFile.lower() == exe_lower:
                    n += 1
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
            return n
        finally:
            _k32.CloseHandle(snap)
else:
    def process_info(pid):
        try:
            os.kill(pid, 0)          # POSIX 上 signal 0 才是真正的探活
            return True, ""
        except (OSError, ProcessLookupError):
            return False, ""

    def count_processes(exe_lower):
        return 0


# ---------------------------------------------------------------- 转录解析

_transcript_cache = {}   # sessionId -> jsonl 路径，避免每次轮询都 glob 18 个目录


def find_transcript(session_id):
    hit = _transcript_cache.get(session_id)
    if hit and os.path.exists(hit):
        return hit
    found = glob.glob(os.path.join(PROJECTS_DIR, "*", session_id + ".jsonl"))
    if not found:
        return None
    _transcript_cache[session_id] = found[0]
    return found[0]


def _codex_state_map(state):
    """把 Tokenarium 的状态映射到 Claude Zoo 的状态集合。"""
    return {
        "working": "working",
        "thinking": "thinking",
        "waiting": "waiting",
        "paused": "dozing",
        "sleeping": "dozing",
        "quiet": "quiet",
        "error": "stuck",
    }.get((state or "").strip().lower(), "thinking")


def _codex_cards(now):
    """读取 Codex 本地线程作为 Zoo 卡片显示。"""
    try:
        from tokenarium_server import collect as codex_collect, STALE_ACTIVE_SECONDS
    except Exception:
        return []

    try:
        snap = codex_collect()
    except Exception:
        return []

    cards = []
    for c in snap.get("sessions", []):
        # Tokenarium 是最近历史视图，Zoo 只展示仍在场的会话。
        # 保留已加载（持有 writer lock）的线程，以及锁暂不可见但仍有新活动
        # 的进行中任务；已退出的完成/中断/失败记录不再占一只动物。
        age = c.get("activityAgeSeconds", c.get("ageSeconds"))
        recently_running = (
            c.get("turnStatus") == "inProgress"
            and isinstance(age, (int, float))
            and 0 <= age <= STALE_ACTIVE_SECONDS
        )
        if not c.get("locked") and not recently_running:
            continue
        ts = c.get("lastSeen")
        tokens = int(c.get("tokens", 0) or 0)
        state = _codex_state_map(c.get("state"))
        cards.append({
            "id": "codex:" + str(c.get("id", "?")),
            "kind": "codex",
            "pid": None,
            "name": str(c.get("name", "")).strip() or "Codex 会话",
            "title": c.get("name", "") or "Codex 会话",
            "cwd": c.get("cwd", ""),
            "folder": c.get("folder", ""),
            "version": "",
            "entrypoint": "codex",
            "startedAt": None,
            "state": state,
            "emoji": c.get("emoji", "🧪"),
            "verb": c.get("activity", "") or state,
            "hint": "Codex 本地线程",
            "say": "",
            "ask": c.get("ask", "") or "",
            "done": state in ("waiting", "dozing", "quiet"),
            "agents": [],
            "model": c.get("model", ""),
            "branch": c.get("branch", ""),
            "context": tokens,
            "ctxPct": min(100.0, tokens / CONTEXT_MAX * 100),
            "ctxLabel": "%s · %s" % (kilo(tokens), "codex tokens"),
            "idle": c.get("ageSeconds"),
            "sizeMB": 0,
            "helpers": min(CODA_MAX_HELPERS, int(c.get("activeHelpers", c.get("helpers", 0)) or 0)),
            "beats": c.get("beats", [0] * 12),
            "lastTs": ts if isinstance(ts, (int, float)) else None,
            "sayTs": ts if isinstance(ts, (int, float)) else None,
        })
    return cards


def infer_session_kind(meta, image=""):
    """根据会话元数据 / 进程名判断这只卡片归属。

    常规 Claude Code 走 claude/node；Codex 会话通常会在 entrypoint/name 上带 codex 字样，
    即便进程名变成 python 也要保留可见性。未知情况仍按 claude 兜底。
    """
    entrypoint = str(meta.get("entrypoint") or "").lower()
    name = str(meta.get("name") or "").lower()
    image_name = str(image or "").lower()
    if "codex" in entrypoint or "codex" in name or "codex" in image_name:
        return "codex"
    return "claude"


def is_session_process(meta, image):
    """判断 .claude/sessions 里的 pid 是否对应当前可追踪会话。

    关键点：优先按进程名识别（兼容历史行为），再按元数据中的 codex 标记兜底。
    """
    if "claude" in image or "node" in image:
        return True
    if "codex" in str(meta.get("entrypoint", "")).lower() or "codex" in str(meta.get("name", "")).lower():
        return True
    return False


def tail_entries(path, limit_bytes=TAIL_BYTES):
    """从文件尾部读一段，解析成 entry 列表（丢掉可能被截断的第一行）。"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > limit_bytes:
            f.seek(size - limit_bytes)
            f.readline()             # 这行大概率是半截的，扔掉
        raw = f.read()
    out = []
    for line in raw.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# 工具名 -> (emoji, 人话)
TOOL_FLAVOR = {
    "Bash":          ("⌨️", "敲命令行"),
    "PowerShell":    ("⌨️", "敲命令行"),
    "Read":          ("📖", "读文件"),
    "Write":         ("✍️", "写文件"),
    "Edit":          ("✂️", "改代码"),
    "NotebookEdit":  ("📓", "改 notebook"),
    "Grep":          ("🔍", "满仓库找线索"),
    "Glob":          ("🗂️", "翻文件夹"),
    "WebFetch":      ("🌐", "上网抓页面"),
    "WebSearch":     ("🌐", "上网搜"),
    "Agent":         ("👥", "指挥小弟"),
    "Task":          ("👥", "指挥小弟"),
    "TodoWrite":     ("📝", "列清单"),
    "Artifact":      ("🎨", "发布作品"),
    "Skill":         ("🎩", "掏出技能"),
    "AskUserQuestion": ("🙋", "举手提问"),
}

HINT_KEYS = ("description", "command", "file_path", "pattern", "query",
             "prompt", "url", "skill", "path")

# MCP 工具的原始名长这样：mcp__Claude_Browser__javascript_tool。直接甩到卡片上
# 又长又丑，这里拆出后半截翻成人话；没收录的就把下划线换成空格，至少不刺眼。
MCP_VERB = {
    "navigate": "开网页", "read_page": "读页面", "get_page_text": "扒页面文字",
    "javascript_tool": "在页面里跑脚本", "computer": "点鼠标敲键盘",
    "find": "在页面里找东西", "form_input": "填表单",
    "read_console_messages": "看控制台", "read_network_requests": "看网络请求",
    "preview_start": "起预览", "preview_stop": "关预览", "preview_logs": "翻预览日志",
    "resize_window": "改窗口尺寸", "show_widget": "画个图", "read_me": "读说明书",
}


def summarize_tool(name, tool_input):
    if isinstance(name, str) and name.startswith("mcp__"):
        parts = name.split("__")
        tool = parts[2] if len(parts) > 2 else name
        emoji, verb = "🔌", MCP_VERB.get(tool, tool.replace("_", " "))
    else:
        emoji, verb = TOOL_FLAVOR.get(name, ("🔧", name))
    hint = ""
    if isinstance(tool_input, dict):
        for k in HINT_KEYS:
            v = tool_input.get(k)
            if isinstance(v, str) and v.strip():
                hint = " ".join(v.split())
                break
    return emoji, verb, hint[:180]


def _blocks_text(v):
    """tool_result 的 content 可能是字符串，也可能是一串 text 块，统一抻成一段文本。"""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return " ".join(b.get("text", "") for b in v if isinstance(b, dict))
    return ""


def scan_agents(entries):
    """找出「派出去了但还没回来」的后台小弟，返回它们的任务描述。

    坑在这儿：后台 Agent 是「发射后即返回」的——tool_result 立刻就回一句
    "Async agent launched successfully"，所以拿 tool_use / tool_result 配对
    根本判断不了它跑没跑完（结果永远是秒回的）。真正的完工信号是后面单独
    追加的一条 <task-notification>，里面的 <tool-use-id> 指回当初那次调用。
    所以配对的两头是：tool_use.id  ←→  通知里的 <tool-use-id>。

    只扫转录尾巴。好处是不会误报：万一某个老会话的发射和通知都被甩出窗口，
    我们两头都看不见，也就不会硬说人家"还在跑"（早期版本就栽在这——49MB 的
    转录里结果明明回来了，只因为在尾部窗口外，被当成了还在跑）。
    """
    desc = {}                 # tool_use_id -> 任务描述
    launched, finished = set(), set()
    for e in entries:
        msg = e.get("message") or {}
        content = msg.get("content")

        # 完工通知是一条纯文本的 user 消息
        text = content if isinstance(content, str) else ""
        if isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        if "<task-notification>" in text:
            for tid in re.findall(r"<tool-use-id>(.*?)</tool-use-id>", text):
                finished.add(tid)

        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"):
                desc[b.get("id")] = " ".join(
                    ((b.get("input") or {}).get("description") or "后台任务").split())[:60]
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in desc:
                # 必须先确认这条结果对应的调用【本身就是 Agent 工具】（tool_use_id 在 desc 里），
                # 再看它是不是"异步已发射"。否则任何工具的输出只要正文里恰好出现
                # "Async agent launched" 这句话，就会被当成派出去一个小弟——排查时把这句
                # 打印到终端，就足以让自己那张卡凭空多出俩小弟。
                # 前台 agent 的结果是它本人的产出，不带这句，等于当场跑完，不算在跑。
                if "Async agent launched" in _blocks_text(b.get("content")):
                    launched.add(b.get("tool_use_id"))

    return [desc.get(i, "后台任务") for i in launched if i not in finished]


def read_session(meta):
    """把一个 session 的元数据 + 转录尾巴，熬成一份前端要的状态。"""
    sid = meta["sessionId"]
    path = find_transcript(sid)
    now = time.time()
    image = str(meta.get("entrypoint", ""))
    kind = infer_session_kind(meta, image=image)

    card = {
        "id": sid,
        "kind": kind,
        "pid": meta.get("pid"),
        "name": meta.get("name") or sid[:8],
        "title": "",
        "cwd": meta.get("cwd", ""),
        "folder": os.path.basename(meta.get("cwd", "").rstrip("\\/")) or meta.get("cwd", ""),
        "version": meta.get("version", ""),
        "entrypoint": meta.get("entrypoint", ""),
        "startedAt": meta.get("startedAt", 0) / 1000 if meta.get("startedAt") else None,
        "state": "unknown",
        "emoji": "❔",
        "verb": "查无此人",
        "hint": "",
        "say": "",
        "model": "",
        "branch": "",
        "context": 0,
        "ctxPct": 0,
        "ctxLabel": "",
        "idle": None,
        "sizeMB": 0,
        "helpers": 0,
        "beats": [],
        "lastTs": None,
        "sayTs": None,
        "ask": "",
        "done": False,      # 这一轮是不是真答完了（stop_reason == end_turn）
        "agents": [],       # 派出去还没回来的后台小弟（任务描述）
    }

    if not path:
        card["state"] = "notranscript"
        card["verb"] = "还没开口"
        return card

    card["sizeMB"] = round(os.path.getsize(path) / 1048576, 1)
    idle = now - os.path.getmtime(path)
    card["idle"] = round(idle, 1)

    try:
        entries = tail_entries(path)
    except OSError:
        entries = []
    # 转录尾部混着 last-prompt / custom-title / ai-title / mode 这类记账行，
    # 它们没有 timestamp 也没有 message，绝不能拿来当“最后一条消息”。
    msgs = [e for e in entries
            if e.get("type") in ("user", "assistant") and isinstance(e.get("message"), dict)]

    # 不过 title 这两行很有用：那是这个 session 自己起的标题
    for e in reversed(entries):
        if e.get("type") == "custom-title" and e.get("customTitle"):
            card["title"] = e["customTitle"][:60]
            break
        if e.get("type") == "ai-title" and e.get("aiTitle") and not card["title"]:
            card["title"] = e["aiTitle"][:60]

    # 你最后一句话。Claude Code 专门记了一行 last-prompt，比从 user 消息里翻靠谱得多
    # ——user 消息可能是图片、工具结果、或 /skill 展开后的一大段模板，都不是你说的话。
    card["ask"] = next((" ".join(e["lastPrompt"].split())[:300] for e in reversed(entries)
                        if e.get("type") == "last-prompt" and e.get("lastPrompt")), "")

    if not msgs:
        card["state"] = "quiet"
        card["verb"] = "刚落座"
        return card

    # 心跳条：最近 6 分钟按 12 个 30 秒的桶统计消息密度
    beats = [0] * 12
    for e in msgs:
        ts = parse_ts(e.get("timestamp"))
        if ts is None:
            continue
        delta = now - ts
        if 0 <= delta < 360:
            beats[11 - int(delta // 30)] += 1
    card["beats"] = beats

    last = msgs[-1]
    card["lastTs"] = next((t for t in (parse_ts(e.get("timestamp")) for e in reversed(msgs))
                           if t is not None), None)
    card["branch"] = next((e["gitBranch"] for e in reversed(msgs) if e.get("gitBranch")), "")

    # idle 要按「最后一条消息」的时间算，而不是文件 mtime。转录文件常被 mode /
    # ai-title 这类记账记录碰新，mtime 会骗人——session 其实 14 天没说过话了，
    # 却因为文件刚被写过而躲过 24 小时过滤。用真正的最后消息时间才准。
    if card["lastTs"] is not None:
        idle = now - card["lastTs"]
        card["idle"] = round(idle, 1)

    # 往回找最近一条 assistant，取模型和上下文用量
    for e in reversed(msgs):
        msg = e.get("message") or {}
        if e.get("type") == "assistant" and msg.get("model"):
            card["model"] = msg["model"]
            u = msg.get("usage") or {}
            card["context"] = (u.get("cache_read_input_tokens", 0)
                               + u.get("cache_creation_input_tokens", 0)
                               + u.get("input_tokens", 0))
            break

    # 往回找最近一句 assistant 的人话，当作台词
    for e in reversed(msgs):
        msg = e.get("message") or {}
        if e.get("type") != "assistant":
            continue
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            continue
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        text = " ".join(text.split())
        if text:
            card["say"] = text[:400]
            card["sayTs"] = parse_ts(e.get("timestamp"))
            break

    # 先算小弟再分类：一个「话说完了但还有小弟在外面跑」的会话，等的是小弟不是你，
    # classify 要靠这个把「等你回话」和「等小弟回来」分开。
    card["agents"] = scan_agents(entries)
    card["helpers"] = len(card["agents"])

    classify(card, last, idle)

    card["ctxPct"] = min(100.0, card["context"] / CONTEXT_MAX * 100)
    card["ctxLabel"] = "%s / 1M · 卷宗 %sMB" % (kilo(card["context"]), card["sizeMB"])
    return card


def classify(card, last, idle):
    """靠最后一条 entry 的形状判断这货此刻在干嘛。"""
    kind = last.get("type")
    msg = last.get("message") or {}
    blocks = msg.get("content")
    stale = idle > DOZE_AFTER

    if kind == "assistant" and isinstance(blocks, list):
        tool = next((b for b in blocks if b.get("type") == "tool_use"), None)
        if tool:
            emoji, verb, hint = summarize_tool(tool.get("name", "?"), tool.get("input"))
            card.update(state="stuck" if idle > 300 else "working",
                        emoji=emoji, verb=verb, hint=hint)
            if card["state"] == "stuck":
                card["verb"] = verb + "…（很久没动静）"
            return

        # 到这儿 = 这条只有文字/思考，没动手。但「没动手」不等于「说完了」！
        # 一个回合会被拆成好几条记录（thinking → text → tool_use），三条的
        # stop_reason 都是 tool_use。轮询要是正好停在中间那条，光看"有没有
        # tool_use 块"会把干得正欢的判成"等你回话"。真正的权威信号是 stop_reason：
        #   end_turn  = 整轮答完了，球在你那边
        #   tool_use  = 话说到一半，马上要动手
        done = msg.get("stop_reason") == "end_turn"
        card["done"] = done
        if done and card.get("agents"):
            # 话是说完了，但派出去的小弟还没回来——它等的是小弟，不是你。
            # 这种不能显示"等你回话"，会骗你跑去敲键盘。也不让它打盹：活还没干完。
            # 单独一个状态：不能复用 waiting，那个是招手动作——看着像在叫你过来，
            # 而这会儿它根本不需要你。awaiting 是站着张望，手垂下不动。
            n = len(card["agents"])
            card.update(state="awaiting", emoji="👥",
                        verb="等 %d 个小弟回来" % n,
                        hint=" ｜ ".join(card["agents"])[:180])
        elif done:
            card.update(state="dozing" if stale else "waiting",
                        emoji="😴" if stale else "🙋",
                        verb="打盹中" if stale else "等你回话")
        elif stale:
            # 没说完却半天没动静 = 被你按了 Esc，或者中途断了。这种也是等你发话。
            card.update(state="waiting", emoji="✋", verb="半路停下 · 等你发话")
        else:
            card.update(state="thinking", emoji="⚡", verb="话说一半，正要动手")
        return

    if kind == "user":
        is_result = isinstance(blocks, list) and any(
            b.get("type") == "tool_result" for b in blocks)
        card.update(state="dozing" if stale else "thinking",
                    emoji="😴" if stale else "💭",
                    verb="打盹中" if stale else ("消化工具结果" if is_result else "读你的新指令"))
        return

    card.update(state="dozing" if stale else "thinking",
                emoji="😴" if stale else "💭",
                verb="打盹中" if stale else "在想")


# ---------------------------------------------------------------- Cursor
# Cursor 把对话存在一个 437MB 的 SQLite 里（globalStorage/state.vscdb）：
#   composerHeaders 表        每场对话一行，带 lastUpdatedAt / 标题 / 上下文占用
#   cursorDiskKV              composerData:<id> 是对话状态，bubbleId:<id>:<bid> 是单条消息
#   各 workspace 自己的库      composer.composerData 里的 lastFocusedComposerIds
#                             = 那个窗口当前开着哪一场
# 全程 mode=ro 只读打开，绝不动 Cursor 的数据。

def _ro(path, timeout=2.0):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                           uri=True, timeout=timeout)


def _jload(v):
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


def _uri_to_path(u):
    """把 file:///d%3A/temp 这种还原成 d:\\temp。"""
    if not isinstance(u, str) or not u.startswith("file:///"):
        return ""
    p = urllib.parse.unquote(u[8:])
    return p.replace("/", "\\")


# 几个看着有用、实测全是坑的字段（每秒采样蹲出来的，别再踩）：
#
#   generatingBubbleIds       永远是空的，这个字段不落盘。拿它判断“正在生成”是死代码。
#   status                    正在跑的时候值是 "aborted"。完全不可信。
#   composerHeaders.lastUpdatedAt
#                             一轮里只写一次。实测消息从 1 条涨到 11 条的整个过程，
#                             它卡在同一个值不动 —— 拿它算 idle 会把干得正欢的
#                             对话显示成“打盹中”。
#   workspace 库的 lastFocusedComposerIds
#                             只在开关窗口时写，三小时没动过，记的 id 跟当前对话对不上。
#
# 真正实时的只有一个：fullConversationHeadersOnly 里最后一条 bubble 的 createdAt。
# 每次工具调用都会追加一条并立刻落盘，所以判据跟 Claude Code 那边一样，纯看时间戳。

CURSOR_BUSY = 25          # 秒内冒出过新消息 = 正在生成


def cursor_cards(now):
    if not os.path.exists(CURSOR_DB) or not count_processes("cursor.exe"):
        return []

    cutoff = int((now - 24 * 3600) * 1000)      # 只看最近 24 小时动过的
    try:
        con = _ro(CURSOR_DB)
    except sqlite3.Error:
        return []

    try:
        rows = con.execute(
            """SELECT composerId, workspaceId, lastUpdatedAt, value
               FROM composerHeaders
               WHERE lastUpdatedAt >= ? AND isSubagent = 0
               ORDER BY lastUpdatedAt DESC LIMIT 40""", (cutoff,)).fetchall()

        # 一个 workspace 只留一张卡：最近动过的那场（rows 已按时间倒序）
        best = {}
        for cid, ws, ua, val in rows:
            head = _jload(val) or {}
            # 新开的对话要过一会儿才有名字（实测起手是空的）。刚动过的无名对话
            # 要留着，否则你刚发出去的那句话在卡片上是看不见的。
            if not (head.get("name") or "").strip():
                if ws == "empty-window" or (now - (ua or 0) / 1000) > 600:
                    continue
            best.setdefault(ws or cid, (cid, ua, head))

        cards = []
        for cid, ua, head in sorted(best.values(), key=lambda x: -x[1])[:8]:
            try:
                cards.append(_cursor_card(con, cid, ua, head, now))
            except Exception:
                continue
        return cards
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _cursor_card(con, cid, ua, head, now):
    last_ts = (ua or 0) / 1000
    ws = head.get("workspaceIdentifier") or {}
    cwd = _uri_to_path(((ws.get("uri") or {}).get("external"))) or \
        ((ws.get("uri") or {}).get("fsPath")) or ""

    card = {
        "id": "cursor:" + cid, "kind": "cursor", "pid": None,
        "name": (head.get("name") or "新对话").strip()[:60],
        "title": (head.get("name") or "新对话").strip()[:60],
        "cwd": cwd, "folder": os.path.basename(cwd.rstrip("\\/")) or cwd,
        "version": "", "entrypoint": "cursor", "startedAt": (head.get("createdAt") or 0) / 1000 or None,
        "model": head.get("unifiedMode") or "",
        "branch": "", "context": 0,
        "ctxPct": float(head.get("contextUsagePercent") or 0),
        "idle": round(now - last_ts, 1) if last_ts else None,
        "sizeMB": 0, "helpers": int(head.get("numSubComposers") or 0),
        "beats": [0] * 12, "lastTs": last_ts or None, "sayTs": None,
        # done=None：Cursor 的 bubble 里没有 stop_reason 这种字段，它到底答完没有
        # 我们是真的看不出来，所以标"不知道"，不能像 Claude 那边一样给 True/False
        "say": "", "ask": "", "done": None, "hint": head.get("subtitle") or "",
        "state": "dozing", "emoji": "😴", "verb": "打盹中",
    }
    add, rm, nfiles = (head.get("totalLinesAdded") or 0,
                       head.get("totalLinesRemoved") or 0,
                       head.get("filesChangedCount") or 0)
    card["ctxLabel"] = "%.0f%% · 改了 %d 个文件 +%d/-%d" % (card["ctxPct"], nfiles, add, rm)

    # 每次都重新解析。不能拿 header 的 lastUpdatedAt 当缓存键——Cursor 会在
    # 不动 header 时间戳的情况下改 composerData（比如 aborted 之后又跑完变
    # completed），缓存会把旧状态钉死。700KB 的 JSON 解析在 15 秒间隔下不值一提。
    row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                      ("composerData:" + cid,)).fetchone()
    data = _jload(row[0]) if row else None
    if not data:
        return card

    heads = data.get("fullConversationHeadersOnly") or []
    beats = [0] * 12
    newest = None
    for h in heads[-80:]:
        t = parse_ts(h.get("createdAt"))
        if t is None:
            continue
        if newest is None or t > newest:
            newest = t
        delta = now - t
        if 0 <= delta < 360:
            beats[11 - int(delta // 30)] += 1
    card["beats"] = beats

    # 最后一条 bubble 的时间才是真的"刚动过"，比 header 的 lastUpdatedAt 灵敏得多
    if newest and (not card["lastTs"] or newest > card["lastTs"]):
        card["lastTs"] = newest
        card["idle"] = round(now - newest, 1)

    idle = card["idle"]
    if idle is None:
        card.update(state="quiet", emoji="📄", verb="还没开始")
    elif idle < CURSOR_BUSY:
        card.update(state="working", emoji="🔨", verb="正在生成")
    elif idle < DOZE_AFTER:
        card.update(state="waiting", emoji="🙋", verb="等你下一句")
    else:
        card.update(state="dozing", emoji="😴", verb="打盹中")

    # 最后一条消息当台词（bubble 单独存一行，只读这一行，不碰其它 16000 条）
    if heads:
        bid = heads[-1].get("bubbleId")
        row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                          ("bubbleId:%s:%s" % (cid, bid),)).fetchone()
        b = _jload(row[0]) if row else None
        if b:
            card["say"] = " ".join((b.get("text") or "").split())[:400]
            card["sayTs"] = parse_ts(b.get("createdAt"))
    return card


def kilo(n):
    return "%.0fk" % (n / 1000) if n >= 100000 else (
        "%.1fk" % (n / 1000) if n >= 1000 else str(n or 0))


def parse_ts(s):
    if not isinstance(s, str) or len(s) < 19:
        return None
    try:
        # 形如 2026-07-31T05:12:18.123Z，UTC
        import calendar
        import datetime as dt
        base = dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        return calendar.timegm(base.timetuple())
    except ValueError:
        return None


def collect():
    cards = []
    ghosts = 0
    for p in sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        pid = meta.get("pid")
        if pid:
            alive, image = process_info(int(pid))
            # 名字对不上 = pid 被系统回收后复用了，不是我们的 session
            if (not alive) or (image and not is_session_process(meta, image.lower())):
                ghosts += 1
                continue
        try:
            cards.append(read_session(meta))
        except Exception as exc:                       # 单只翻车不能连累整屋
            k = infer_session_kind(meta)
            cards.append({"id": meta.get("sessionId", "?"), "name": meta.get("name", "?"),
                          "kind": k,
                          "state": "broken", "emoji": "💥", "verb": "读取失败",
                          "hint": str(exc)[:120], "cwd": meta.get("cwd", ""), "title": "",
                          "folder": "", "beats": [], "say": "", "sayTs": None, "model": "",
                          "branch": "", "context": 0, "idle": None, "sizeMB": 0,
                          "helpers": 0, "pid": meta.get("pid"), "startedAt": None,
                          "version": "", "entrypoint": "", "lastTs": None})
    now = time.time()
    try:
        cards += cursor_cards(now)
    except Exception:
        pass                                     # Cursor 那边翻车，不能连累 Claude 这边

    try:
        cards.extend(_codex_cards(now))
    except Exception:
        pass

    # 先给命中 PIN 的卡片打标，占位卡稍后补上
    placeholders = apply_pins(cards)

    # 读不到「最后一条消息时间」的不显示：开了窗口但一句话没说过的野生会话，
    # 没什么可监控的，占地方。（被 PIN 住的除外，那是说好永远显示的。）
    wild = sum(1 for c in cards if c.get("pin") is None and c.get("lastTs") is None)
    cards = [c for c in cards if c.get("pin") is not None or c.get("lastTs") is not None]

    # 静置太久的不显示（主要是 Cursor 攒下的一堆闲置对话）；被 PIN 住的也永远留着。
    hidden = sum(1 for c in cards if c.get("pin") is None
                 and c.get("idle") is not None and c["idle"] > MAX_IDLE_SEC)
    cards = [c for c in cards if c.get("pin") is not None
             or c.get("idle") is None or c["idle"] <= MAX_IDLE_SEC]
    cards += placeholders

    # 一律按「最后动过的时间」倒序，pin 的不再享受置顶特权——pin 现在只保证
    # 「永远显示」（不被 24 小时门槛滤掉），谁在最上面单纯看谁刚动过。
    # 注意这里按 lastTs（一个固定时间戳）排，不是按 idle 秒数——idle 每秒都在变，
    # 拿它排序会让卡片每轮轮询都互相插队，看着一直在抖。
    def sort_key(c):
        ts = c.get("lastTs")
        return (0, -ts, "") if ts else (1, 0, c.get("name", ""))   # 没消息的沉底

    cards.sort(key=sort_key)

    # 重建可开列表：每张卡都能开自己的目录，pin 的额外带上配置里的网址
    OPENABLE.clear()
    for c in cards:
        if c.get("cwd") or c.get("pinUrl"):
            OPENABLE[c["id"]] = {"cwd": c.get("cwd", ""), "url": c.get("pinUrl", "")}
        c["canOpen"] = bool(c.get("cwd"))
    return {"now": now, "sessions": cards, "ghosts": ghosts, "hidden": hidden,
            "wild": wild, "movie": pick_movie(now)}


# ---------------------------------------------------------------- HTTP

# 页面上的折叠/展开/关闭按钮通过这里跟桌面外壳（zoo_app.py）通气。
# 外壳的主线程轮询 UI["want"]，变了就用 Win32 改窗口大小。
UI = {"want": "max"}


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/sessions"):
            self._json(collect())
            return
        if self.path.startswith("/api/open"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cid = (qs.get("id") or [""])[0]
            self._json({"ok": open_card(cid, (qs.get("what") or [""])[0])})
            return
        if self.path.startswith("/api/launch-sessions"):
            self._json({"ok": launch_sessions()})
            return
        if self.path.startswith("/api/ui"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            want = (qs.get("set") or [None])[0]
            if want in ("min", "max", "close"):
                if want == "min" and UI["want"] == "min":
                    want = "max"                 # 同一个按钮来回切
                UI["want"] = want
            self._json({"want": UI["want"]})
            return
        try:
            with open(os.path.join(HERE, "zoo.html"), "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404, "zoo.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass          # 别把控制台刷爆


def main():
    # Windows 控制台默认 GBK，打 emoji 会炸
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://127.0.0.1:%d" % PORT
    snapshot = collect()
    print("🐾 Claude Zoo  ->  %s" % url)
    print("   现在屋里有 %d 只（另有 %d 个陈旧的 pid 文件已忽略）"
          % (len(snapshot["sessions"]), snapshot["ghosts"]))
    for c in snapshot["sessions"]:
        print("   %s %-12s %s %s" % (c["emoji"], c["name"], c["verb"], c["folder"]))
    print("   Ctrl+C 关门")
    if "--no-open" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 打烊")


if __name__ == "__main__":
    main()

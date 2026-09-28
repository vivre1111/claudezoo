# Tokenarium · 令牌生态园

一个只观察本机 Codex thread 元数据的小型夜间生态园。

它会显示本地任务的忙闲状态、工作目录、模型、最近活动和子 agent 数量，
并根据当天日期与园内气氛推荐一部电影。不会读取 `auth.json`，不会恢复、修改、
归档或删除任何任务，也不会请求云端接口。

## 启动

```powershell
python tokenarium.py
```

浏览器将打开 <http://127.0.0.1:8788>。也可以运行紧凑桌面版：

```powershell
.\.venv\Scripts\python.exe tokenarium_desktop.py
```

桌面版是独立的无边框 `pywebview` 应用；WebView2 只作为 Windows 系统渲染组件，不会打开 Edge 浏览器窗口。
它会贴在右下角、
避开任务栏；多屏时会躲到鼠标不在的屏幕，并在你操作同屏内容时淡出。
默认尺寸为 430×720，顶部的 `—` 可在完整视图和 54px 观察条之间切换。
协作伙伴正在运行且有 writer lock 时会始终显示；已完成、失败或中断的伙伴只保留 30 分钟，
超过后从父任务的计数中隐藏。没有 writer lock 的 `inProgress` 记录超过 20 分钟也会被视为陈旧记录。

只看隐私安全的 JSON 快照：

```powershell
python tokenarium.py --snapshot
```

## 本地数据边界

- 只读打开最新的 `~/.codex/state_*.sqlite`。
- 只读打开最新的 `~/.codex/thread_history_*.sqlite`。
- 只检查 `~/.codex/thread-writer-locks/<thread-id>.lock` 是否存在。
- 默认不展示输入提示、模型回答、命令正文、工具参数或日志正文。
- HTTP 服务仅绑定 `127.0.0.1`。
- “打开文件夹”按钮只接受当前快照中后端登记过的 thread id，不能传入任意路径。

默认显示最近 72 小时内的根任务，以及仍有 writer lock 的任务。子 agent 作为其父任务
的协作伙伴数量显示，不会挤成独立卡片。

## 测试

```powershell
python -m unittest -v test_tokenarium.py
```

测试使用临时合成数据库，不复制真实 Codex 会话。

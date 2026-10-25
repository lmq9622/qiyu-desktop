"""
栖语 (Qiyu) - 桌面客户端 (.exe 入口)
======================================
双击运行，无需命令行，自带浏览器内核
======================================
"""

import os
import sys
import time
import subprocess
import threading

# ============ PyInstaller 多进程兼容 ============
if getattr(sys, 'frozen', False):
    # 打包后的 exe 需要这个，否则子进程会递归启动自己
    import multiprocessing
    multiprocessing.freeze_support()


# ============ 资源路径处理（兼容 PyInstaller） ============
def get_resource_path(relative_path: str) -> str:
    """获取资源文件路径，兼容 PyInstaller 打包"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(__file__), relative_path)


def get_icon_path() -> str:
    """窗口/任务栏图标：优先 exe 同目录 sidecar assets，其次打包内的 assets。"""
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "assets", "icon.ico"))
    candidates.append(get_resource_path("assets/icon.ico"))
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""


def _apply_windows_window_icon():
    """pywebview 在部分 Windows 主题下不画 start(icon=...)，用 Win32 再兜底设置一次。"""
    if sys.platform != "win32":
        return
    icon = get_icon_path()
    if not icon:
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        # 等窗口创建完成
        hwnd = 0
        for _ in range(60):
            hwnd = user32.FindWindowW(None, "栖语 Qiyu · AI 伴侣")
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
        hicon = user32.LoadImageW(0, icon, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not hicon:
            return
        WM_SETICON = 0x80
        user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)
        user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)
    except Exception:
        pass


# ============ 日志（兼容 windowed 模式） ============
LOG_DIR = os.path.join(os.path.expanduser('~'), '.ai_companion')
LOG_PATH = os.path.join(LOG_DIR, 'client.log')
os.makedirs(LOG_DIR, exist_ok=True)


def rotate_log(path: str, max_bytes: int = 20 * 1024 * 1024) -> None:
    """日志超过阈值就先改名成 .1，避免 backend.log 无限长（实测到过 94 MB）。"""
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + '.1')
    except Exception:
        pass

# 启动器也读打包进来的 .env：端口等只在 .env 里改了的话，本进程（等 /health、开窗口）
# 和后端子进程必须看到同一份 QIYU_PORT，否则会「后端起在别的端口、窗口永远等不到」
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(get_resource_path(".env"))
except Exception:
    pass

def log(msg: str):
    """写入日志文件（windowed 模式下不能用 print/input）"""
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f'[{time.strftime("%H:%M:%S")}] {msg}\n')
    except Exception:
        pass


# ============ 后端进程管理 ============
backend_proc = None

def start_backend_process():
    """用子进程启动 FastAPI 后端（uvicorn 需要独立进程才能跑 asyncio）"""
    global backend_proc
    
    env = os.environ.copy()
    env['AI_COMPANION_BACKEND'] = '1'
    env['PYTHONPATH'] = get_resource_path('.')
    env['AI_COMPANION_LOG_DIR'] = LOG_DIR
    
    # 在打包后的 exe 中，sys.executable 就是这个 exe 本身
    # 通过环境变量让它进入后端模式而不是前端模式
    exe_path = sys.executable
    if getattr(sys, "frozen", False):
        cmd = [exe_path]
    else:
        # 源码模式：python.exe 不带参数只会进 REPL，必须显式带上 client.py
        # 才会走 __main__ → AI_COMPANION_BACKEND=1 → run_backend()
        cmd = [exe_path, os.path.abspath(__file__)]

    log(f"启动后端进程: {' '.join(cmd)}")
    
    # Windows 下隐藏控制台窗口
    startupinfo = None
    if sys.platform == 'win32':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
    
    backend_proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        startupinfo=startupinfo,
        cwd=get_resource_path('.'),
    )
    
    # 启动一个线程读取后端日志
    def read_backend_log():
        try:
            backend_log_path = os.path.join(LOG_DIR, 'backend.log')
            rotate_log(backend_log_path)
            with open(backend_log_path, 'a', encoding='utf-8') as logf:
                for line in backend_proc.stdout:
                    decoded = line.decode('utf-8', errors='replace').rstrip()
                    logf.write(f'[{time.strftime("%H:%M:%S")}] {decoded}\n')
                    logf.flush()
        except Exception:
            pass
    
    import threading
    threading.Thread(target=read_backend_log, daemon=True).start()
    
    return backend_proc


def is_backend_ready() -> bool:
    """检查后端服务是否就绪"""
    try:
        import urllib.request
        port = int(os.getenv("QIYU_PORT", "8765"))
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
        return True
    except Exception:
        return False


def kill_backend():
    """结束后端进程"""
    global backend_proc
    if backend_proc and backend_proc.poll() is None:
        backend_proc.terminate()
        try:
            backend_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            backend_proc.kill()
        backend_proc = None


# ============ 后端模式入口 ============
def run_backend():
    """子进程入口：只跑 FastAPI 后端"""
    log("[后端模式] 启动 FastAPI 服务...")
    
    # 重新定向 stdout/stderr 到日志文件
    backend_log_path = os.path.join(LOG_DIR, 'backend.log')
    rotate_log(backend_log_path)
    sys.stdout = open(backend_log_path, 'a', encoding='utf-8', buffering=1)
    sys.stderr = sys.stdout
    
    os.environ['AI_COMPANION_CLIENT_MODE'] = '1'
    sys.path.insert(0, get_resource_path('.'))
    
    import uvicorn
    from demo import app
    port = int(os.getenv("QIYU_PORT", "8765"))

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


# ============ 前端模式入口 ============
def run_frontend():
    """主进程入口：启动后端 + 打开窗口"""
    log("=" * 50)
    log("栖语 Qiyu 桌面客户端启动")
    log("=" * 50)
    
    # 启动后端子进程
    start_backend_process()
    
    # 等待服务就绪：首启要加载 torch/embedding，实测 18–27 秒，给到 120 秒；
    # 后端进程如果已经退出就立刻失败，不干等。
    log("等待后端服务就绪...")
    ready = False
    for i in range(240):  # 最多等 120 秒
        if backend_proc is not None and backend_proc.poll() is not None:
            log(f"❌ 后端进程已退出（exit={backend_proc.returncode}）")
            break
        if is_backend_ready():
            ready = True
            log("✅ 后端服务已就绪")
            break
        time.sleep(0.5)
    
    if not ready:
        log("❌ 后端服务启动超时")
        kill_backend()
        show_error_and_exit("后端服务启动失败，请检查日志:\n" + LOG_DIR)
        return
    
    # 启动桌面窗口
    log("启动桌面窗口...")
    try:
        import webview

        # Windows 任务栏：显式设置 AppUserModelID，避免窗口图标跟 python.exe 归成一组
        if sys.platform == 'win32':
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.qiyu.desktop")
            except Exception:
                pass

        window = webview.create_window(
            title="栖语 Qiyu · AI 伴侣",
            url=f"http://127.0.0.1:{int(os.getenv('QIYU_PORT', '8765'))}/",
            width=1400,
            height=900,
            min_size=(1000, 700),
            text_select=True,
        )
        # 再过一道 Win32 图标兜底，避免 pywebview/Windows 主题不显示图标
        threading.Thread(target=_apply_windows_window_icon, daemon=True).start()
        
        webview.start(
            debug=False,
            gui='edgechromium',
            icon=get_icon_path() or None,
        )
        
    except ImportError:
        log("⚠️ pywebview 未安装，使用系统浏览器")
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{int(os.getenv('QIYU_PORT', '8765'))}/")
        # windowed 模式下不能用 input()，直接休眠等待
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    
    # 窗口关闭后结束后端
    log("窗口已关闭，结束后端进程...")
    kill_backend()
    log("已退出")


def show_error_and_exit(message: str):
    """显示错误弹窗并退出"""
    log(f"错误: {message}")
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "栖语 Qiyu 启动失败", 0x10)
        else:
            import tkinter.messagebox
            tkinter.messagebox.showerror("栖语 Qiyu 启动失败", message)
    except Exception:
        pass
    sys.exit(1)


# ============ 主入口 ============
if __name__ == "__main__":
    # 检测当前是前端模式还是后端模式
    if os.environ.get('AI_COMPANION_BACKEND') == '1':
        run_backend()
    else:
        run_frontend()

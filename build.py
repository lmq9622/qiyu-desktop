#!/usr/bin/env python3
"""
栖语 (Qiyu) - PyInstaller 打包脚本
====================================
运行: python build.py
输出: dist/Qiyu/Qiyu.exe
====================================
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# ============ 配置 ============
PROJECT_ROOT = Path(__file__).parent
ENTRY_SCRIPT = PROJECT_ROOT / "client.py"
OUTPUT_NAME = os.environ.get("QIYU_OUTPUT_NAME", "Qiyu")
ICON_FILE = PROJECT_ROOT / "assets" / "icon.ico"  # 如果有图标的话
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
SPEC_FILE = PROJECT_ROOT / f"{OUTPUT_NAME}.spec"
VERSION = "1.0.0"   # M9 打包版本（每次打包顺延 +0.0.1）


def package_dir() -> Path:
    """打包产物目录 = 用户拿到的那一层（sidecar 必须放这里）。

    - onedir（默认）：dist/Qiyu/，Qiyu.exe 就在这里；
    - onefile：dist/，Qiyu.exe 在这里。
    运行时 config/wechaty/models 都是按「exe 同目录」找的（见 config/__init__.py、
    channels/wechaty_channel.py、runtime.get_models_dir），放错一层就会静默降级。
    """
    onefile = str(os.environ.get("QIYU_ONEFILE", "")).strip() in ("1", "true", "yes")
    return DIST_DIR if onefile else DIST_DIR / OUTPUT_NAME

# ============ 检查环境 ============
def check_env():
    """检查打包环境"""
    print("=" * 60)
    print("栖语 - 打包检查")
    print("=" * 60)
    
    # 检查 PyInstaller
    try:
        import PyInstaller
        print(f"[OK] PyInstaller {PyInstaller.__version__}")
    except ImportError:
        print("[FAIL] PyInstaller 未安装")
        print("  请运行: pip install pyinstaller")
        return False
    
    # 检查 pywebview
    try:
        import webview
        ver = getattr(webview, '__version__', 'unknown')
        print(f"[OK] pywebview {ver}")
    except ImportError:
        print("[WARN] pywebview 未安装，桌面窗口将无法使用")
        print("  请运行: pip install pywebview")
    
    # 检查入口文件
    if not ENTRY_SCRIPT.exists():
        print(f"[FAIL] 入口文件不存在: {ENTRY_SCRIPT}")
        return False
    print(f"[OK] 入口文件: {ENTRY_SCRIPT}")
    
    # 检查关键资源
    resources = [
        "characters/__init__.py",
        "config/__init__.py",
        "config/settings.yaml",
        "config/routes.yaml",
        "memory/__init__.py",
        "rag/__init__.py",
        "wechat/__init__.py",
        "gateway/__init__.py",
        "gateway/router.py",
        "gateway/static/index.html",
        "demo.py",
    ]
    resources = [
        "characters/__init__.py",
        "config/__init__.py",
        "config/settings.yaml",
        "config/routes.yaml",
        "gateway/__init__.py",
        "gateway/router.py",
        "gateway/static/index.html",
        "demo.py",
    ]
    
    all_ok = True
    for res in resources:
        path = PROJECT_ROOT / res
        if path.exists():
            print(f"[OK] {res}")
        else:
            print(f"[MISS] {res}")
            all_ok = False
    
    print("=" * 60)
    return all_ok


# ============ 构建命令 ============
def build():
    """执行 PyInstaller 打包"""
    
    # 清理旧构建
    if BUILD_DIR.exists():
        print("清理旧构建目录...")
        shutil.rmtree(BUILD_DIR)
    
    if SPEC_FILE.exists():
        print("清理旧 spec 文件...")
        SPEC_FILE.unlink()
    
    # 收集 --add-data 参数 (Windows 用 ; 分隔)
    sep = ";" if sys.platform == "win32" else ":"
    add_data = []
    
    # 资源目录映射
    resources = [
        ("characters", "characters"),
        ("config", "config"),
        ("memory", "memory"),
        ("rag", "rag"),
        ("wechat", "wechat"),
        ("gateway/static", "gateway/static"),
        ("gateway/router.py", "gateway"),
        ("gateway/__init__.py", "gateway"),
        ("demo.py", "."),
    ]
    resources = [
        ("characters", "characters"),
        ("config", "config"),
        ("channels", "channels"),
        ("gateway/static", "gateway/static"),
        ("gateway/router.py", "gateway"),
        ("gateway/__init__.py", "gateway"),
        ("demo.py", "."),
        ("assets/icon.ico", "assets"),
        ("assets/icon.png", "assets"),
        ("assets/logo.png", "assets"),
    ]
    
    for src, dst in resources:
        src_path = PROJECT_ROOT / src
        if src_path.exists():
            add_data.append(f"--add-data")
            add_data.append(f"{src_path}{sep}{dst}")
    
    # Hidden imports (PyInstaller 可能分析不到的模块)
    hidden_imports = [
        "fastapi",
        "uvicorn",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.protocols.websockets.websockets_impl",
        "pydantic",
        "pydantic.deprecated.decorator",
        "httpx",
        "loguru",
        "yaml",
        "dotenv",
        "characters",
        "config",
        "gateway.router",
        "anyio._backends._asyncio",
        "starlette.middleware.cors",
        "starlette.staticfiles",
        # 外部通讯通道层（channels）
        "channels",
        "channels.base",
        "channels.store",
        "channels.registry",
        "channels.clawbot",
        "channels.wechaty_channel",
        "channels.wechatauto_channel",
        "psutil",
        "channels.placeholders",
        # M1/M3/M4：companion 包 + runtime Provider 层（函数级惰性 import 需要显式收集）
        "companion",
        "companion.constants",
        "companion.state",
        "companion.models",
        "companion.settings",
        "companion.relations",
        "companion.conv",
        "companion.emotions",
        "companion.behavior",
        "companion.active",
        "companion.llm",
        "companion.topic",
        "companion.scheduler",
        "runtime",
        "runtime.providers",
        "runtime.hardware",
        "runtime.manager",
        "runtime.realtime",
        "runtime.minimindo",
        "runtime.minimindo.model_omni",
        "runtime.minimindo.model_minimind",
        "runtime.memory",
        "runtime.toolagent",
        # M6：Realtime Brain 多组件 / 视觉 / 语音 / Avatar / 平台 / DB / 日志 / 性能 / 并发（函数级惰性 import）
        "runtime.logging_setup",
        "runtime.perf",
        "runtime.concurrency",
        "runtime.benchmark",
        "runtime.vision",
        "runtime.stt",
        "runtime.tts",
        "runtime.embedding_provider",
        "runtime.avatar",
        "runtime.platform",
        "runtime.db",
        "runtime.main_brain",
        "companion.pipeline",
        # Tool Agent 联网工具箱（tools/ 目录被 .gitignore 忽略，必须显式收集）
        "tools",
        "tools.web",
        "tools.install_models",
        # ClawBot 二维码生成（demo.py 运行时惰性导入，PyInstaller 静态分析不到）
        "qrcode",
        "PIL",
        "PIL.Image",
        # wechatauto 本机接管（运行时惰性导入）
        "wechatauto",
        "wechatauto.wx",
        "wechatauto.db",
        "wechatauto.guia",
        "wechatauto.media",
        "wechatauto.sender",
        "wechatauto.msgs",
        "wechatauto.ui",
        "wechatauto.uia",
        "wechatauto.utils",
        "uiautomation",
        "comtypes",
        "pyautogui",
        "zstandard",
        "imageio_ffmpeg",
    ]

    # 排除运行时不需要的重型可选依赖（条件导入被过度跟踪，会拉入 torch/transformers 等）
    excluded_modules = [
        "torch", "torchvision", "torchaudio", "functorch", "accelerate",
        "transformers", "sentence_transformers", "datasets", "tokenizers",
        "safetensors", "huggingface_hub", "hf_xet", "onnxruntime",
        "IPython", "traitlets", "pyreadline3", "prompt_toolkit",
        "matplotlib", "mpl_toolkits", "pandas", "scipy", "sklearn",
        "nltk", "networkx", "sympy", "joblib", "narwhals", "fsspec",
        "ddtrace", "sentry_sdk", "opentelemetry", "sqlalchemy",
        "clickhouse_connect", "openai", "anthropic", "tiktoken",
        "grpc", "grpc_tools", "google.protobuf", "google.auth",
        "polars", "duckdb", "ibis", "dask", "modin", "tensorflow",
    ]
    
    # 构建命令
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", OUTPUT_NAME,
        # 默认 onedir：启动直接跑，不用每次把 285MB 解压到临时目录（onefile 首次启动 10–20s）。
        # 要单文件分发时设 QIYU_ONEFILE=1。
        *(["--onefile"] if str(os.environ.get("QIYU_ONEFILE", "")).strip() in ("1", "true", "yes") else ["--onedir"]),
        "--windowed",          # 不显示控制台窗口
        "--noconfirm",         # 不确认覆盖
        "--clean",             # 清理临时文件
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
    ]
    
    # 添加 hidden imports
    for imp in hidden_imports:
        cmd.extend(["--hidden-import", imp])

    # qy3fengine：桌面端角色包引擎（vendor 目录，不走 pip 安装）
    # hidden-import 单独用不可靠（实测 _internal 里没有），所以把整棵源码树
    # 作为 data 放进 _internal/qy3fengine —— 运行时它就在 sys.path 上，import 得到。
    if (PROJECT_ROOT / "qy3fengine" / "__init__.py").exists():
        cmd.extend(["--paths", str(PROJECT_ROOT)])
        cmd.extend(["--add-data", f"{PROJECT_ROOT / 'qy3fengine'}{os.pathsep}qy3fengine"])
        for _imp in ("qy3fengine",
                     "qy3fengine.container",
                     "qy3fengine.core.export",
                     "qy3fengine.core.import_",
                     "qy3fengine.core.validate",
                     "qy3fengine.rag.memory_recall",
                     "qy3fengine.rag.vector_store"):
            cmd.extend(["--hidden-import", _imp])

    # 添加排除模块
    for mod in excluded_modules:
        cmd.extend(["--exclude-module", mod])

    # 启用 UPX 压缩（若可用）
    upx_dir = PROJECT_ROOT / "tools" / "upx"
    if (upx_dir / "upx.exe").exists():
        cmd.extend(["--upx-dir", str(upx_dir)])
    
    # 添加资源
    cmd.extend(add_data)
    
    # 图标
    if ICON_FILE.exists():
        cmd.extend(["--icon", str(ICON_FILE)])
    
    # 入口文件
    cmd.append(str(ENTRY_SCRIPT))
    
    print("\n" + "=" * 60)
    print("开始打包...")
    print("=" * 60)
    print("命令:")
    print(" \\\n".join(cmd))
    print("=" * 60 + "\n")
    
    # 执行
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    
    if result.returncode == 0:
        print("\n" + "=" * 60)
        print("打包成功!")
        print("=" * 60)
        exe_path = DIST_DIR / OUTPUT_NAME / f"{OUTPUT_NAME}.exe"
        if exe_path.exists():
            print(f"输出文件: {exe_path}")
            print(f"文件大小: {exe_path.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            # onefile 模式
            exe_path = DIST_DIR / f"{OUTPUT_NAME}.exe"
            if exe_path.exists():
                print(f"输出文件: {exe_path}")
                print(f"文件大小: {exe_path.stat().st_size / 1024 / 1024:.1f} MB")
        # 复制 Wechaty 网关目录（gateway.js / package.json / install.bat，不含 node_modules）
        try:
            src_gw = PROJECT_ROOT / "wechaty"
            dst_gw = package_dir() / "wechaty"
            if src_gw.exists():
                dst_gw.mkdir(parents=True, exist_ok=True)
                for fname in ("gateway.js", "package.json", "install.bat"):
                    f = src_gw / fname
                    if f.exists():
                        shutil.copy2(f, dst_gw / fname)
                print(f"[OK] Wechaty 网关目录已复制到 {dst_gw}（运行 install.bat 安装依赖后即可用）")
        except Exception as e:
            print(f"[WARN] 复制 Wechaty 网关目录失败: {e}")
        # M5：可独立更新的 sidecar 目录（runtime/backends/models/assets/config/tools）
        try:
            create_sidecars()
        except Exception as e:
            print(f"[WARN] 创建 sidecar 目录失败: {e}")
        print("=" * 60)
        return True
    else:
        print(f"\n[ERROR] 打包失败，返回码: {result.returncode}")
        return False


# ============ 快速模式（用 spec 文件） ============
def spec_points_here(spec_text: str, root: Path) -> bool:
    """spec 中入口脚本的绝对路径是否位于 root 之下（大小写、斜杠方向不敏感）"""
    import re
    m = re.search(r"Analysis\(\s*\[\s*['\"]([^'\"]+)['\"]", spec_text)
    if not m:
        return False
    norm = lambda p: os.path.normcase(os.path.abspath(p))
    entry, base = norm(m.group(1)), norm(str(root))
    return entry == base or entry.startswith(base + os.sep)


def build_fast():
    """使用已有 spec 文件快速构建（第二次以后）"""
    if not SPEC_FILE.exists():
        print("没有找到 .spec 文件，请先运行完整构建")
        return False
    # spec 里写的是生成它那台机器的绝对路径。仓库里带的旧 spec 指向服务器版目录
    # （D:/Codex projects/.../ai-companion），在那台机器上 --fast 不会报错，
    # 而是把**服务器版的代码**打进桌面版 exe。路径不在本项目下就拒绝，改走完整构建。
    if not spec_points_here(SPEC_FILE.read_text(encoding="utf-8", errors="ignore"), PROJECT_ROOT):
        print(f"[FAIL] {SPEC_FILE.name} 里的路径不指向本项目（{PROJECT_ROOT}），"
              "--fast 会打包别处的代码。请运行完整构建：python build.py")
        return False
    
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC_FILE), "--noconfirm", "--clean"]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    return result.returncode == 0


def create_sidecars():
    """M5（规格§10）：构建可独立更新的 sidecar 目录。
    Qiyu.exe + runtime/ + backends/ + models/ + assets/ + config/ + tools/ + wechaty/
    模型 / 后端 / 配置不物理塞进 EXE：更新 Vulkan backend 不用重新下载整个模型。"""
    base = package_dir()
    base.mkdir(parents=True, exist_ok=True)
    # 版本标记
    try:
        (base / "VERSION.txt").write_text(f"{VERSION}\n", encoding="utf-8")
    except Exception as e:
        print(f"[WARN] 写 VERSION.txt 失败: {e}")
    # 可独立更新的目录（附说明）
    sidecar_notes = {
        "runtime": "runtime 资源目录（可独立更新）。",
        "backends": "推理后端目录（llama.cpp 等，可独立更新）。",
        "models": "本地模型目录（GGUF 等，可独立更新）。",
        "assets": "资源目录（图标等，可独立更新）。",
        "tools": "辅助工具目录（可独立更新）。",
    }
    for name, note in sidecar_notes.items():
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        readme = d / "README.txt"
        if not readme.exists():
            readme.write_text(note + "\n", encoding="utf-8")
        print(f"[OK] sidecar: {d}")
    # M6：把模型安装器与联网工具箱放进 sidecar tools/（可独立更新）
    try:
        tool_srcs = [PROJECT_ROOT / "tools" / "web.py", PROJECT_ROOT / "tools" / "install_models.py"]
        tools_dir = base / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        for _f in tool_srcs:
            if _f.exists():
                shutil.copy2(_f, tools_dir / _f.name)
        print(f"[OK] sidecar tools: web.py / install_models.py 已复制")
    except Exception as e:
        print(f"[WARN] 复制 tools 工具失败: {e}")
    # 可选：把仓库里已有的权重一起复制进 sidecar（QIYU_COPY_MODELS=1）。
    # 默认不复制：权重 139 MB，放进安装包会让体积从 683 MB 涨到 820 MB+。
    if str(os.environ.get("QIYU_COPY_MODELS", "")).strip() in ("1", "true", "yes"):
        try:
            src_models = PROJECT_ROOT / "models"
            dst_models = base / "models"
            if src_models.exists():
                shutil.copytree(src_models, dst_models, dirs_exist_ok=True)
                total = sum(f.stat().st_size for f in dst_models.rglob("*") if f.is_file())
                print(f"[OK] 已复制 models/（{total / 1024 / 1024:.1f} MB）")
        except Exception as e:
            print(f"[WARN] 复制 models/ 失败: {e}")

    # M6：models/ 说明安装器用法
    try:
        models_readme = base / "models" / "README.txt"
        models_readme.write_text(
            "本地模型目录（可独立更新）。\n"
            "Realtime Brain（MiniMind-O）：运行 tools/install_models.py 下载 Thinker 权重到 models/realtime/。\n"
            "Main Brain：把 GGUF 放到 models/main/（或配置远程 API）。\n",
            encoding="utf-8")
    except Exception as e:
        print(f"[WARN] 写 models README 失败: {e}")
    # 用户可编辑配置副本（exe 启动时优先读 exe 同目录 config/）
    cfg_dir = base / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("settings.yaml", "routes.yaml"):
        src = PROJECT_ROOT / "config" / fname
        if src.exists():
            dst = cfg_dir / fname
            if not dst.exists():
                shutil.copy2(src, dst)
            print(f"[OK] 配置副本: {dst}")
    if ICON_FILE.exists():
        try:
            shutil.copy2(ICON_FILE, base / "assets" / "icon.ico")
            for _extra in ("icon.png", "logo.png"):
                _src = PROJECT_ROOT / "assets" / _extra
                if _src.exists():
                    shutil.copy2(_src, base / "assets" / _extra)
        except Exception as e:
            print(f"[WARN] 复制图标失败: {e}")
    print("[OK] sidecar 结构完成: runtime/ backends/ models/ assets/ config/ tools/")


# ============ 主入口 ============
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="栖语 打包工具")
    parser.add_argument("--fast", action="store_true", help="使用已有 spec 快速构建")
    parser.add_argument("--check-only", action="store_true", help="只检查环境不打包")
    args = parser.parse_args()
    
    if args.check_only:
        check_env()
        sys.exit(0)
    
    if not check_env():
        print("\n环境检查未通过，请修复上述问题后重试")
        sys.exit(1)
    
    if args.fast:
        success = build_fast()
    else:
        success = build()
    
    sys.exit(0 if success else 1)

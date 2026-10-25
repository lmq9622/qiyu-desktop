"""微信语音本地转写（可选，非硬依赖）。

优先级：
1) iLink voice_item.text —— 微信自带的 ASR 转写，直接用（主路径，零成本）；
2) 本地 ASR（sherpa-onnx + 小型中文模型）—— 仅当 voice_item 没有转写文本、
   且本机装了 sherpa-onnx、且能找到模型目录时才会尝试。任何一步失败都安静降级，
   不影响主流程，也不拖慢消息处理。

用法：
    from .wechat_voice import transcribe_voice_item
    text = transcribe_voice_item(voice_item)   # 失败返回 ""
"""

import base64
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from loguru import logger

_engine = None
_engine_lock = threading.Lock()


def _model_dir() -> str:
    """模型目录：优先环境变量 QIYU_ASR_MODEL，其次 models/asr，再其次 data/asr"""
    env = os.environ.get("QIYU_ASR_MODEL", "").strip()
    if env and os.path.isdir(env):
        return env
    candidates = []
    try:
        from runtime import get_models_dir
        candidates.append(get_models_dir("asr"))
    except Exception:
        candidates.append(Path(__file__).resolve().parent.parent / "models" / "asr")
    try:
        from pathutil import get_data_dir
        candidates.append(get_data_dir() / "asr")
    except Exception:
        candidates.append(Path(__file__).resolve().parent.parent / "data" / "asr")
    for base in candidates:
        if not base.exists():
            continue
        for sub in sorted(base.iterdir()):
            if sub.is_dir() and _asr_model_file(sub):
                return str(sub)
        # 直接放在目录下
        if _asr_model_file(base):
            return str(base)
    return ""


def _asr_model_file(md) -> str:
    """识别可加载的模型文件：paraformer 双文件(encoder/decoder) 或单文件(model/model.int8)。"""
    md = Path(md)
    if (md / "encoder.onnx").exists() and (md / "decoder.onnx").exists():
        return "paraformer"
    for fname in ("model.int8.onnx", "model.onnx"):
        if (md / fname).exists():
            return str(md / fname)
    return ""


def local_asr_available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
        return bool(_model_dir())
    except Exception:
        return False


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            import sherpa_onnx
            md = _model_dir()
            if not md:
                logger.info("[语音] 未找到 sherpa-onnx 模型目录（可设 QIYU_ASR_MODEL 指向模型目录）")
                _engine = None
                return None
            mfile = _asr_model_file(md)
            if mfile == "paraformer":
                _engine = sherpa_onnx.OfflineRecognizer.from_paraformer(
                    encoder=os.path.join(md, "encoder.onnx"),
                    decoder=os.path.join(md, "decoder.onnx"),
                    tokens=os.path.join(md, "tokens.txt"),
                )
            else:
                _engine = sherpa_onnx.OfflineRecognizer.from_paraformer(
                    paraformer=mfile,
                    tokens=os.path.join(md, "tokens.txt"),
                )
            logger.success("[语音] 本地 ASR（sherpa-onnx paraformer-zh）就绪")
        except Exception as e:
            logger.warning(f"[语音] 本地 ASR 初始化失败（不影响聊天）: {e}")
            _engine = None
        return _engine


def _ensure_wav(src: str) -> str:
    """把任意音频（silk/amr/mp3/ogg）转成 16k 单声道 wav；已经是 wav 就直接用。
    需要 ffmpeg（或 sherpa 自带工具）；没有 ffmpeg 时返回空。"""
    import wave
    try:
        with wave.open(src, "rb") as w:
            return src
    except Exception:
        pass
    return _ffmpeg_to_wav(src)


def _wav_16k_mono(path: str):
    """把 wav 读成 16bit / 16k / 单声道 的裸 PCM（sherpa-onnx 只吃 16k 单声道）。

    TTS（CosyVoice）吐出来的是 24k wav，手机录音多是 16k，浏览器还可能给 44.1k——
    以前这里 assert 采样率，非 16k 直接抛异常被吞掉，表现就是「语音发出去却转不出字」。
    现在不依赖 ffmpeg：多声道取平均，非 16k 用线性插值重采样。读不了/非 16bit 返回 None。
    """
    import wave
    import numpy as np
    try:
        with wave.open(path, "rb") as w:
            ch, sr, sw, n = w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes()
            if sw != 2 or n <= 0:
                return None
            raw = w.readframes(n)
    except Exception:
        return None
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != 16000 and len(a) > 1:
        dst = max(1, int(round(len(a) * 16000.0 / float(sr))))
        a = np.interp(np.arange(dst) / 16000.0, np.arange(len(a)) / float(sr), a)
    return np.asarray(np.clip(np.round(a), -32768, 32767), dtype=np.int16).tobytes()


def _ffmpeg_to_wav(src: str) -> str:
    """非 wav（silk/amr/mp3/ogg）走 ffmpeg 转 16k 单声道；没有 ffmpeg 返回空串。"""
    tool = ""
    for tool in ("ffmpeg", "ffmpeg.exe"):
        try:
            subprocess.run([tool, "-version"], capture_output=True, timeout=5)
            break
        except Exception:
            tool = ""
    if not tool:
        return ""
    out = src + ".wav"
    try:
        subprocess.run(
            [tool, "-y", "-i", src, "-ar", "16000", "-ac", "1", out],
            capture_output=True, timeout=60,
        )
        return out if os.path.exists(out) else ""
    except Exception:
        return ""


def _decrypt_aes_ecb(data: bytes, aes_key: str) -> bytes:
    """语音文件用 AES-128-ECB 加密，需要 key 解密"""
    if not aes_key:
        return data
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = base64.b64decode(aes_key)
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        dec = cipher.decryptor()
        out = dec.update(data) + dec.finalize()
        return out
    except Exception as e:
        logger.debug(f"[语音] AES 解密失败: {e}")
        return data


def _transcribe_bytes(raw: bytes, ext: str = "silk") -> str:
    """把字节流转 wav 后交给 sherpa-onnx 识别，失败返回空串"""
    rec = _get_engine()
    if rec is None:
        return ""
    tmp = tempfile.NamedTemporaryFile(suffix="." + (ext or "silk"), delete=False)
    try:
        tmp.write(raw)
        tmp.close()
        wav = _ensure_wav(tmp.name)
        if not wav:
            return ""
        pcm = _wav_16k_mono(wav)
        if not pcm:
            return ""
        s = rec.create_stream()
        import numpy as np
        s.accept_waveform(16000, np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0)
        rec.decode_stream(s)
        text = (s.result.text or "").strip()
        return text
    except Exception as e:
        logger.debug(f"[语音] 本地识别失败: {e}")
        return ""
    finally:
        try:
            tmp.close()
            for p in (tmp.name, tmp.name + ".wav"):
                if p and os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass


def transcribe_voice_item(voice_item: dict) -> str:
    """从 iLink voice_item 里尽力转写：
    - 优先 voice_item.text（微信自带 ASR）；
    - 否则尝试下载语音（media/url + aes_key）→ 解密 → 本地 sherpa-onnx。
    任何失败返回 ""，由调用方降级为 [语音]。"""
    if not isinstance(voice_item, dict):
        return ""
    text = str(voice_item.get("text") or "").strip()
    if text:
        return text
    # 尝试媒体下载
    media = voice_item.get("media") or {}
    url = str(media.get("url") or media.get("cdn_url") or voice_item.get("url") or "").strip()
    aes_key = str(media.get("aes_key") or voice_item.get("aes_key") or "").strip()
    if not url or not local_asr_available():
        return ""
    try:
        import requests
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        raw = _decrypt_aes_ecb(r.content, aes_key)
        ext = "silk"
        if url.endswith(".amr"):
            ext = "amr"
        return _transcribe_bytes(raw, ext)
    except Exception as e:
        logger.debug(f"[语音] 下载/识别失败: {e}")
        return ""


def transcribe_audio_file(path: str) -> str:
    """把已下载到本地的音频文件（silk/amr/mp3 等）转 wav 后交给本地 ASR 识别。
    用于 wechatauto 本机接管：MediaDownloader 下载的语音是本地文件（SILK 居多）。
    任何失败返回 ""，由调用方降级为 [语音]。"""
    if not path or not local_asr_available():
        return ""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        ext = Path(path).suffix.lstrip(".") or "silk"
        return _transcribe_bytes(raw, ext)
    except Exception as e:
        logger.debug(f"[语音] 本地文件识别失败: {e}")
        return ""
def transcribe_voice_bytes(raw: bytes, ext: str = "silk") -> str:
    """公开入口：把语音字节流转写为文本（供 STTProvider 统一接口调用）。

    失败返回 ""，不抛异常；调用方应降级为 [语音]。
    """
    if not raw or not local_asr_available():
        return ""
    try:
        return _transcribe_bytes(raw, ext)
    except Exception as e:
        logger.debug(f"[语音] 字节转写失败: {e}")
        return ""

import os, time, json, threading
from flask import Blueprint, request
from urllib.parse import unquote

# 延迟导入，避免在 app 初始化早期出现循环引用
try:
    from app import MUSIC_DIR, PIPE_NAME, ALLOWED, safe_path, gather_all_tracks, PLAYLIST, CURRENT_INDEX, AUTO_PLAYING, AUTO_THREAD
except Exception:
    # 在解释器加载早期（被 app 导入时）可能还未完全定义，这里先占位；运行时会由真正的模块变量替换
    MUSIC_DIR = ""  # type: ignore
    PIPE_NAME = ""  # type: ignore
    ALLOWED = set()  # type: ignore
    def safe_path(rel):
        raise RuntimeError("safe_path 未就绪")
    def gather_all_tracks(path):
        return []
    PLAYLIST = []
    CURRENT_INDEX = -1
    AUTO_PLAYING = False
    AUTO_THREAD = None

player_bp = Blueprint('player', __name__)

# 以上变量在 app 中已统一定义，这里仅引用，不重新创建新的独立副本

def send_mpv(cmd: dict):
    with open(PIPE_NAME, "wb") as pipe:
        pipe.write((json.dumps(cmd) + "\n").encode("utf-8"))

def mpv_request(cmd: dict, wait: float = 0.05, timeout: float = 0.8) -> dict:
    send_mpv(cmd)
    time.sleep(wait)
    # 读取响应
    start = time.time()
    while time.time() - start < timeout:
        try:
            with open(PIPE_NAME, "r", encoding="utf-8") as pipe:
                line = pipe.readline()
                if line:
                    try:
                        return json.loads(line)
                    except:
                        return {}
        except Exception:
            time.sleep(0.05)
    return {}

def auto_play_loop():
    """复用 app 中的 auto_play_loop 实现，若未能导入则静默退出。"""
    try:
        from app import auto_play_loop as real_loop
        return real_loop()
    except Exception:
        print("[WARN] auto_play_loop 未可用")
        return

@player_bp.route('/play', methods=['POST'])
def play():
    global PLAYLIST, CURRENT_INDEX, AUTO_PLAYING, AUTO_THREAD
    rel = request.form.get("path", "")
    try:
        abs_path_clicked = os.path.normcase(os.path.normpath(safe_path(rel)))
        all_tracks = gather_all_tracks(MUSIC_DIR)
        def norm(p): return os.path.normcase(os.path.normpath(p))
        matched_index = next((i for i, p in enumerate(all_tracks) if norm(p) == abs_path_clicked), None)
        if matched_index is None:
            raise ValueError("歌曲未出现在播放列表中")
        PLAYLIST = all_tracks
        CURRENT_INDEX = matched_index
        AUTO_PLAYING = True
        if AUTO_THREAD is None or not AUTO_THREAD.is_alive():
            AUTO_THREAD = threading.Thread(target=auto_play_loop, daemon=True)
            AUTO_THREAD.start()
        current_path = PLAYLIST[CURRENT_INDEX]
        send_mpv({"command": ["loadfile", current_path, "replace"]})
        # 使用主应用提供的线程安全写入
        try:
            from app import set_current_playing
            set_current_playing(current_path)
        except Exception as e:
            print('[WARN] set_current_playing 调用失败:', e)
        return "OK"
    except Exception as e:
        return f"ERROR: {e}", 400

@player_bp.route('/volume', methods=['POST'])
def volume():
    level_raw = request.form.get("level", "")
    try:
        level = int(unquote(level_raw))
        if not 0 <= level <= 100:
            raise ValueError("音量应在 0–100 之间")
        send_mpv({"command": ["set_property", "volume", level]})
        return "OK"
    except Exception as e:
        return f"ERROR: {e}", 400

@player_bp.route('/progress')
def progress():
    try:
        cur = mpv_request({"command": ["get_property", "playback-time"]}).get("data", 0)
        dur = mpv_request({"command": ["get_property", "duration"]}).get("data", 0)
        return {"status": "OK", "current": int(cur or 0), "duration": int(dur or 0)}
    except Exception as e:
        return {"status": "ERROR", "info": str(e)}

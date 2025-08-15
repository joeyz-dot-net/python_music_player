import os, time, json, threading, subprocess, psutil
from urllib.parse import unquote
from flask import Flask, request, render_template


app = Flask(__name__, template_folder=".")
MUSIC_DIR = r"Z:"
PIPE_NAME = r"\\.\pipe\mpv-pipe"
ALLOWED = {".mp3", ".wav", ".flac"}
MPV_PROCESS = None  # 全局变量保存MPV进程

def build_tree(root):
    abs_root = os.path.abspath(root)
    def walk(path):
        rel = os.path.relpath(path, abs_root).replace("\\", "/")
        node = {
            "name": os.path.basename(path) or "根目录",
            "rel": rel if rel != "." else "",
            "files": [],
            "dirs": []
        }
        # 歌手/目录头像（可选：用缓存或默认）
        artist_photo_url = ""  # 你可以维护一个 artist_image_cache
        # node["artist_photo_url"] = artist_photo_url
        try:
            for entry in sorted(os.listdir(path), key=str.lower):
                full = os.path.join(path, entry)
                if os.path.isdir(full):
                    node["dirs"].append(walk(full))
                else:
                    ext = os.path.splitext(entry)[1].lower()
                    if ext in ALLOWED:
                        rp = os.path.relpath(full, abs_root).replace("\\", "/")
                        # 查找专辑封面缓存
                        key = f"{node['name']}|||{os.path.splitext(entry)[0]}"
                        album_cover_url = album_cover_cache.get(key, "")
                        node["files"].append({
                            "name": entry,
                            "rel": rp,
                            "abs": os.path.abspath(full),
                            "album_cover_url": album_cover_url
                        })
        except:
            pass
        return node
    return walk(abs_root)

def safe_path(rel):
    rel = unquote(rel)
    base = os.path.abspath(MUSIC_DIR)
    target = os.path.abspath(os.path.join(base, rel))
    if not target.startswith(base) or not os.path.exists(target):
        raise ValueError("非法路径")
    return target

# —— MPV IPC：写、读、请求 —— 

def mpv_write(cmd: dict):
    with open(PIPE_NAME, "wb") as pipe:
        pipe.write((json.dumps(cmd) + "\n").encode("utf-8"))

def mpv_read(timeout: float = 0.8) -> dict:
    """
    读取一行 MPV 的 JSON 响应。mpv 对每个命令都会返回一行 JSON。
    """
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

def mpv_request(cmd: dict, wait: float = 0.05, timeout: float = 0.8) -> dict:
    """
    发送命令并读取响应。对 get_property 等查询类命令使用。
    """
    mpv_write(cmd)
    time.sleep(wait)
    return mpv_read(timeout)

def send_mpv(cmd: dict):
    """
    仅发送（不读取响应），适用于 loadfile、stop 等无需等待结果的命令。
    """
    mpv_write(cmd)


@app.route("/")
def index():
    tree = build_tree(MUSIC_DIR)
    current_file = ""
    try:
        with open(os.path.join(os.path.dirname(__file__), "current_playing.json"), "r", encoding="utf-8") as f:
            current_info = json.load(f)
            current_file = current_info.get("abs_path", "")
    except Exception:
        pass
    return render_template("index.html", tree=tree, current_file=current_file)

# —— 播放列表与自动续播 —— 

PLAYLIST = []            # 保留为“真实绝对路径”，用于 loadfile
CURRENT_INDEX = -1
AUTO_PLAYING = False
AUTO_THREAD = None       # 避免重复启动线程

def gather_all_tracks(path):
    tracks = []
    for dirpath, _, files in os.walk(path):
        for f in sorted(files, key=str.lower):
            ext = os.path.splitext(f)[1].lower()
            if ext in ALLOWED:
                tracks.append(os.path.abspath(os.path.join(dirpath, f)))
    return tracks


# 全局/外部变量，确保在模块顶层已定义：
# PLAYLIST: list[str]      播放列表
# CURRENT_INDEX: int       当前播放索引
# AUTO_PLAYING: bool       自动播放开关
# PIPE_NAME: str           MPV IPC 管道/Socket 路径
# send_mpv(cmd: dict)      你已有的 MPV 发送函数（下面示例用 _send_mpv 代替）

def auto_play_loop():
    """
    自动播放主循环：
      1. 等待媒体就绪（避免 time/dur = -1）
      2. 监控播放进度或 eof-reached
      3. 播放结束后自动切歌
      4. 卡住/加载失败时重载或跳过
    """

    global CURRENT_INDEX, PLAYLIST, AUTO_PLAYING, PIPE_NAME

    # 参数可根据需要微调
    POLL_INTERVAL       = 0.5     # 常规轮询间隔（s）
    LOAD_RETRY_INTERVAL = 0.8     # 加载等待间隔（s）
    LOAD_TIMEOUT        = 12.0    # 单曲加载属性最大等待（s）
    STUCK_TIMEOUT       = 15.0    # 播放卡住判定阈值（s）
    END_GUARD           = 0.7     # 认为“接近结束”的剩余时长（s）
    MAX_FAILS           = 2       # 连续加载失败次数阈值

    print("[DEBUG] 自动播放线程已启动")

    # ---- MPV IPC 接口 ----
    def _send_mpv(cmd: dict):
        try:
            with open(PIPE_NAME, "wb") as w:
                w.write((json.dumps(cmd) + "\n").encode("utf-8"))
        except Exception as e:
            print("[ERROR] 写入 MPV 失败:", e)

    def _read_mpv(timeout=1.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(PIPE_NAME, "r", encoding="utf-8", errors="ignore") as r:
                    line = r.readline()
                    if line:
                        return json.loads(line)
            except:
                time.sleep(0.03)
        return {}

    def mpv_get(prop: str, wait=0.05, timeout: float = 0.5):
        _send_mpv({"command": ["get_property", prop]})
        time.sleep(wait)
        resp = _read_mpv(timeout)
        if resp.get("error") == "success":
            return resp.get("data")
        return None

    def mpv_cmd(cmd_list: list):
        _send_mpv({"command": cmd_list})

    # ---- 切歌与重载 ----
    def play_next() -> bool:
        global CURRENT_INDEX
        nonlocal fail_count
        CURRENT_INDEX += 1
        if CURRENT_INDEX >= len(PLAYLIST):
            print("[INFO] 播放列表已播完")
            return False
        next_track = PLAYLIST[CURRENT_INDEX]
        print(f"[INFO] 自动播放下一首：{next_track}")
        mpv_cmd(["loadfile", next_track, "replace"])
        fail_count = 0
        return True

    def reload_current():
        if 0 <= CURRENT_INDEX < len(PLAYLIST):
            track = PLAYLIST[CURRENT_INDEX]
            print(f"[WARN] 重新加载当前曲目：{track}")
            mpv_cmd(["loadfile", track, "replace"])

    # ---- 等待媒体就绪 ----
    def wait_ready() -> bool:
        start = time.time()
        last_log = 0
        while time.time() - start < LOAD_TIMEOUT and AUTO_PLAYING:
            d = mpv_get("duration")
            t = mpv_get("playback-time")
            eof = mpv_get("eof-reached")
            if (isinstance(d, (int, float)) and d > 0) or \
               (isinstance(t, (int, float)) and t >= 0 and eof is False):
                time.sleep(0.1)
                return True
            if time.time() - last_log > 1.0:
                print("[WAITING] 等待 MPV 加载媒体属性...")
                last_log = time.time()
            time.sleep(LOAD_RETRY_INTERVAL)
        return False

    # ---- 监控到曲目结束 ----
    def monitor() -> str:
        if not wait_ready():
            return "reload"

        last_t = -1.0
        last_move_ts = time.time()
        end_flag = False

        while AUTO_PLAYING:
            d   = mpv_get("duration")
            t   = mpv_get("playback-time")
            eof = mpv_get("eof-reached")

            # 强制结束信号
            if eof is True:
                print("[INFO] 检测到 eof-reached")
                return "ended"

            # 属性齐全
            if isinstance(t, (int, float)) and isinstance(d, (int, float)):
                print(f"[TRACE] time={t:.1f} / dur={d:.1f}")
                # 判断前进
                if t > last_t + 0.02:
                    last_t = t
                    last_move_ts = time.time()
                # 接近尾声双确认
                if d - t <= END_GUARD:
                    if end_flag:
                        return "ended"
                    end_flag = True
                else:
                    end_flag = False
            elif isinstance(t, (int, float)):
                # 直播或未知时长
                print(f"[TRACE] time={t:.1f} / dur=unknown")
                if t > last_t + 0.02:
                    last_t = t
                    last_move_ts = time.time()
            else:
                print("[TRACE] time/dur 不可用，等待中...")
                time.sleep(0.3)

            # 卡住判定
            if time.time() - last_move_ts > STUCK_TIMEOUT:
                print("[WARN] 播放停止前进")
                return "stuck"

            time.sleep(POLL_INTERVAL)

        return "abort"

    # ---- 自动播放循环 ----
    fail_count = 0
    while AUTO_PLAYING:
        try:
            if not (0 <= CURRENT_INDEX < len(PLAYLIST)):
                print("[INFO] 当前索引越界，停止播放")
                break

            print(f"[DEBUG] 当前曲目：{PLAYLIST[CURRENT_INDEX]}")

            result = monitor()

            if result == "ended":
                if not play_next():
                    break
                continue

            if result == "stuck":
                print("[WARN] 卡住，尝试重载")
                reload_current()
                if not wait_ready():
                    fail_count += 1
                    if fail_count >= MAX_FAILS:
                        print("[ERROR] 连续加载失败，退出自动播放")
                        break
                    print("[WARN] 跳过当前曲目")
                    if not play_next():
                        break
                continue

            if result == "reload":
                print("[WARN] 属性超时未就绪，重载")
                reload_current()
                if not wait_ready():
                    fail_count += 1
                    if fail_count >= MAX_FAILS:
                        print("[ERROR] 连续加载失败，退出自动播放")
                        break
                    print("[WARN] 跳过当前曲目")
                    if not play_next():
                        break
                continue

            if result == "abort":
                print("[INFO] 自动播放已被外部停止")
                break

        except Exception as ex:
            print("[ERROR] 自动播放异常:", ex)
            time.sleep(1.0)

    print("[DEBUG] 自动播放线程退出")


@app.route("/play", methods=["POST"])
def play():
    global PLAYLIST, CURRENT_INDEX, AUTO_PLAYING, AUTO_THREAD
    rel = request.form.get("path", "")
    print("[DEBUG] 接收到 path 参数:", repr(rel))
    try:
        # 标准化被点击文件路径（用于匹配）
        abs_path_clicked = os.path.normcase(os.path.normpath(safe_path(rel)))
        print("[DEBUG] 解析后的 abs_path:", abs_path_clicked)

        # 生成“绝对路径播放列表”
        all_tracks = gather_all_tracks(MUSIC_DIR)
        print("[DEBUG] 播放列表大小:", len(all_tracks))
        print("[DEBUG] 播放列表预览:", all_tracks[:3])

        # 在列表中定位点击的那一首（用标准化比较）
        def norm(p): return os.path.normcase(os.path.normpath(p))
        matched_index = next((i for i, p in enumerate(all_tracks) if norm(p) == abs_path_clicked), None)
        print("[DEBUG] matched_index:", matched_index)
        if matched_index is None:
            raise ValueError("歌曲未出现在播放列表中")

        # 更新列表与索引
        PLAYLIST = all_tracks
        CURRENT_INDEX = matched_index
        AUTO_PLAYING = True

        # 启动或复用自动播放线程（避免重复）
        if AUTO_THREAD is None or not AUTO_THREAD.is_alive():
            AUTO_THREAD = threading.Thread(target=auto_play_loop, daemon=True)
            AUTO_THREAD.start()
            print("[DEBUG] 自动播放线程已启动")
        else:
            print("[DEBUG] 自动播放线程已在运行")

        # 立即播放当前点击的歌曲
        current_path = PLAYLIST[CURRENT_INDEX]
        send_mpv({"command": ["loadfile", current_path, "replace"]})
        print("[DEBUG] 已发送播放命令:", current_path)

        # 写入当前播放歌曲到 JSON 文件
        current_info = {
            "abs_path": current_path,
            "name": os.path.basename(current_path),
            "timestamp": int(time.time())
        }
        with open(os.path.join(os.path.dirname(__file__), "current_playing.json"), "w", encoding="utf-8") as f:
            json.dump(current_info, f, ensure_ascii=False, indent=2)

        # 不再写入 current_playing.txt
        # with open(os.path.join(os.path.dirname(__file__), "current_playing.txt"), "w", encoding="utf-8") as f:
        #     f.write(current_path)

        return "OK"
    except Exception as e:
        print("[ERROR] 播放失败:", e)
        return f"ERROR: {e}", 400

@app.route("/volume", methods=["POST"])
def volume():
    from urllib.parse import unquote
    level_raw = request.form.get("level", "")
    try:
        level = int(unquote(level_raw))
        if not 0 <= level <= 100:
            raise ValueError("音量应在 0–100 之间")
        send_mpv({"command": ["set_property", "volume", level]})
        print(f"[INFO] 音量设置为: {level}")
        return "OK"
    except Exception as e:
        print("[ERROR] 音量设置失败:", e)
        return f"ERROR: {e}", 400

@app.route("/progress")
def progress():
    try:
        cur = mpv_request({"command": ["get_property", "playback-time"]}).get("data", 0)
        dur = mpv_request({"command": ["get_property", "duration"]}).get("data", 0)
        return {"status": "OK", "current": int(cur or 0), "duration": int(dur or 0)}
    except Exception as e:
        return {"status": "ERROR", "info": str(e)}

def is_mpv_running():
    """检测是否已有 mpv 进程（通过管道名）"""
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            if 'mpv' in proc.info['name'].lower():
                if '--input-ipc-server=\\\\.\\pipe\\mpv-pipe' in ' '.join(proc.info['cmdline']):
                    return True
        except Exception:
            continue
    return False

import requests

SPOTIFY_CLIENT_ID = "7aa37cc01eaa4bff8593c76c61915352"
SPOTIFY_CLIENT_SECRET = "385dcc586ad44304a98099e4694aae85"

def get_spotify_token():
    url = "https://accounts.spotify.com/api/token"
    resp = requests.post(url, data={"grant_type": "client_credentials"},
                         auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET))
    return resp.json().get("access_token", "")

def get_artist_image(artist_name):
    token = get_spotify_token()
    if not token:
        return ""
    url = "https://api.spotify.com/v1/search"
    params = {"q": artist_name, "type": "artist", "limit": 1}
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, params=params, headers=headers)
    items = resp.json().get("artists", {}).get("items", [])
    if items and items[0].get("images"):
        return items[0]["images"][0]["url"]
    return ""

@app.route("/artist_image/<artist>")
def artist_image(artist):
    try:
        url = get_artist_image(artist)
        return {"url": url}
    except Exception as e:
        return {"url": ""}

ALBUM_CACHE_FILE = "static/album_cover_cache.json"

def load_album_cache():
    if os.path.exists(ALBUM_CACHE_FILE):
        with open(ALBUM_CACHE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}

def cache_recently_updated(minutes=60):
    if not os.path.exists(ALBUM_CACHE_FILE):
        print("[DEBUG] 缓存文件不存在")
        return False
    mtime = os.path.getmtime(ALBUM_CACHE_FILE)
    diff = time.time() - mtime
    print(f"[DEBUG] 缓存文件最后修改时间: {time.ctime(mtime)}，距今 {diff:.1f} 秒")
    updated = diff < (minutes * 60)
    print(f"[DEBUG] 缓存是否在{minutes}分钟内更新过: {updated}")
    return updated

album_cover_cache = load_album_cache()

def save_album_cache(cache):
    with open(ALBUM_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

ALBUM_CACHE_UPDATE_INTERVAL = 3600  # 1小时

def update_album_cache_periodically():
    while True:
        # 启动时先判断缓存是否在60分钟内更新过
        if cache_recently_updated(60):
            time.sleep(60)  # 1分钟后再检查
            continue
        # 重新扫描所有歌曲并尝试更新缓存
        for key in list(album_cover_cache.keys()):
            if not album_cover_cache[key]:
                try:
                    artist, track = key.split("|||", 1)
                    token = get_spotify_token()
                    if not token:
                        continue
                    api_url = "https://api.spotify.com/v1/search"
                    params = {
                        "q": f"track:{track} artist:{artist}",
                        "type": "track",
                        "limit": 1
                    }
                    headers = {"Authorization": f"Bearer {token}"}
                    resp = requests.get(api_url, params=params, headers=headers)
                    items = resp.json().get("tracks", {}).get("items", [])
                    if items and items[0].get("album", {}).get("images"):
                        url = items[0]["album"]["images"][0]["url"]
                    else:
                        url = ""
                    album_cover_cache[key] = url
                    save_album_cache(album_cover_cache)
                except Exception:
                    continue
        time.sleep(ALBUM_CACHE_UPDATE_INTERVAL)

# 启动定时线程
threading.Thread(target=update_album_cache_periodically, daemon=True).start()


if __name__ == "__main__":
    # 不再自动启动 mpv，只运行 Flask
    #c:\mpv\mpv.exe --input-ipc-server=\\.\pipe\mpv-pipe --idle=yes --force-window=no
    app.run(host="0.0.0.0", port=8000, debug=True)

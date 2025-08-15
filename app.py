import os, time, json, threading, subprocess, psutil
from urllib.parse import unquote
from flask import Flask, render_template


app = Flask(__name__, template_folder=".")

# 载入配置
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "settings.json")
CONFIG_LOCK = threading.RLock()  # 线程级并发写入锁
def _load_settings_unlocked():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as cf:
            return json.load(cf)
    except Exception:
        return {}

def load_settings():
    """线程安全读取配置 (浅复制)。"""
    with CONFIG_LOCK:
        data = _load_settings_unlocked()
        return json.loads(json.dumps(data))  # 简单复制防止外部修改

def _atomic_write_settings(data: dict):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as wf:
        json.dump(data, wf, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)

def update_settings(patch: dict):
    """线程安全更新顶层键；原子写入。"""
    with CONFIG_LOCK:
        cur = _load_settings_unlocked()
        cur.update(patch)
        _atomic_write_settings(cur)
        return cur

def set_current_playing(abs_path: str):
    info = {
        'abs_path': abs_path,
        'name': os.path.basename(abs_path),
        'timestamp': int(time.time())
    }
    update_settings({'current_playing': info})
    return info

_cfg = load_settings()

MUSIC_DIR = _cfg.get("MUSIC_DIR", r"Z:")
PIPE_NAME = _cfg.get("PIPE_NAME", r"\\.\pipe\mpv-pipe")
ALLOWED = set(_cfg.get("ALLOWED_EXTENSIONS", [".mp3", ".wav", ".flac"]))
MPV_PROCESS = None  # 全局变量保存MPV进程

# 若上述关键字段在 settings.json 中缺失，则写回默认值，保证文件中显式存在
_need_persist = False
if "MUSIC_DIR" not in _cfg:
    _cfg["MUSIC_DIR"] = MUSIC_DIR; _need_persist = True
if "PIPE_NAME" not in _cfg:
    _cfg["PIPE_NAME"] = PIPE_NAME; _need_persist = True
if "ALLOWED_EXTENSIONS" not in _cfg:
    _cfg["ALLOWED_EXTENSIONS"] = sorted(list(ALLOWED)); _need_persist = True
if _need_persist:
    update_settings({
        "MUSIC_DIR": _cfg["MUSIC_DIR"],
        "PIPE_NAME": _cfg["PIPE_NAME"],
        "ALLOWED_EXTENSIONS": _cfg["ALLOWED_EXTENSIONS"],
    })

# 播放列表与自动续播所需全局变量（放在前面，供 player.py 导入时使用）
PLAYLIST = []            # 真实绝对路径
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

def build_tree(root):
    """构建目录树（不再依赖已移除的缓存变量）"""
    abs_root = os.path.abspath(root)

    def walk(path):
        rel = os.path.relpath(path, abs_root).replace("\\", "/")
        node = {
            "name": os.path.basename(path) or "根目录",
            "rel": rel if rel != "." else "",
            "files": [],
            "dirs": []
        }
        try:
            for entry in sorted(os.listdir(path), key=str.lower):
                full = os.path.join(path, entry)
                if os.path.isdir(full):
                    node["dirs"].append(walk(full))
                else:
                    ext = os.path.splitext(entry)[1].lower()
                    if ext in ALLOWED:
                        rp = os.path.relpath(full, abs_root).replace("\\", "/")
                        node["files"].append({
                            "name": entry,
                            "rel": rp,
                            "abs": os.path.abspath(full)
                        })
        except Exception:
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
    current_rel = ""
    # 从 settings.json 读取当前播放
    try:
        cfg_now = load_settings()
        current_file = cfg_now.get('current_playing', {}).get('abs_path', '')
        if current_file and os.path.isabs(current_file) and os.path.exists(current_file):
            try:
                current_rel = os.path.relpath(current_file, MUSIC_DIR).replace("\\", "/")
            except Exception:
                current_rel = ""
    except Exception:
        current_file = ""
    return render_template("index.html", tree=tree, current_file=current_file, current_rel=current_rel)

# 注册 player 蓝图（放在所有依赖定义之后，避免循环导入问题）
from player import player_bp
app.register_blueprint(player_bp)


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

SPOTIFY_CLIENT_ID = _cfg.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = _cfg.get("SPOTIFY_CLIENT_SECRET", "")

# 简单内存缓存：歌手信息（避免频繁请求 Spotify）
_ARTIST_INFO_CACHE = {}
_ARTIST_INFO_TTL = 6 * 3600  # 6 小时

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

def get_artist_full_info(artist_name: str):
    now = time.time()
    key = artist_name.lower().strip()
    v = _ARTIST_INFO_CACHE.get(key)
    if v and now - v['ts'] < _ARTIST_INFO_TTL:
        return v['data']
    token = get_spotify_token()
    if not token:
        return {}
    url = "https://api.spotify.com/v1/search"
    params = {"q": artist_name, "type": "artist", "limit": 1}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=6)
        item = resp.json().get("artists", {}).get("items", [])
        if not item:
            data = {}
        else:
            a = item[0]
            data = {
                "name": a.get("name"),
                "genres": a.get("genres", [])[:5],
                "popularity": a.get("popularity"),
                "followers": a.get("followers", {}).get("total"),
                "image": (a.get("images") or [{}])[0].get("url", "")
            }
        _ARTIST_INFO_CACHE[key] = {"ts": now, "data": data}
        return data
    except Exception:
       return {}


@app.route("/artist_image/<artist>")
def artist_image(artist):
    try:
        url = get_artist_image(artist)
        return {"url": url}
    except Exception as e:
        return {"url": ""}

@app.route("/artist_info/<artist>")
def artist_info(artist):
    try:
        data = get_artist_full_info(artist)
        return {"status": "OK", "data": data}
    except Exception:
        return {"status": "ERROR", "data": {}}

# 新增：专辑封面接口，优先尝试Spotify API
@app.route("/album_cover/<artist>/<track>")
def album_cover(artist, track):
    try:
        token = get_spotify_token()
        if not token:
            return {"url": ""}

        api_url = "https://api.spotify.com/v1/search"
        headers = {"Authorization": f"Bearer {token}"}

        def search(q):
            resp = requests.get(api_url, params={"q": q, "type": "track", "limit": 1}, headers=headers, timeout=6)
            j = resp.json()
            items = j.get("tracks", {}).get("items", [])
            if items and items[0].get("album", {}).get("images"):
                return items[0]["album"]["images"][0]["url"]
            return ""

        artist_clean = artist.strip()
        track_clean = track.strip()

        url = ""
        # 优先：同时包含 artist + track（若 artist 非空）
        if artist_clean:
            url = search(f"track:{track_clean} artist:{artist_clean}")
        # 回退：只用 track 搜索
        if not url:
            url = search(f"track:{track_clean}") or search(track_clean)

        return {"url": url}
    except Exception as e:
        return {"url": ""}




if __name__ == "__main__":
    # 不再自动启动 mpv，只运行 Flask，端口与 host 从配置读取
    app.run(host=_cfg.get("FLASK_HOST", "0.0.0.0"), port=_cfg.get("FLASK_PORT", 8000), debug=_cfg.get("DEBUG", False))

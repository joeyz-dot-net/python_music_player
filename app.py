import os, json, threading, time, subprocess
from flask import Flask, render_template, jsonify

APP = Flask(__name__, template_folder='.')

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'settings.json')
_LOCK = threading.RLock()

def _load_raw():
	try:
		with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
			return json.load(f)
	except Exception:
		return {}

def load_settings():
	with _LOCK:
		return json.loads(json.dumps(_load_raw()))

def update_settings(patch: dict):
	with _LOCK:
		cur = _load_raw()
		cur.update(patch)
		tmp = CONFIG_PATH + '.tmp'
		with open(tmp, 'w', encoding='utf-8') as w:
			json.dump(cur, w, ensure_ascii=False, indent=2)
		os.replace(tmp, CONFIG_PATH)
		return cur

cfg = load_settings()
MUSIC_DIR = cfg.get('MUSIC_DIR', 'Z:')
if len(MUSIC_DIR) == 2 and MUSIC_DIR[1] == ':' and MUSIC_DIR[0].isalpha():
	MUSIC_DIR += '\\'
MUSIC_DIR = os.path.abspath(MUSIC_DIR)
PIPE_NAME = cfg.get('PIPE_NAME', r'\\.\pipe\mpv-pipe')
ALLOWED = set(cfg.get('ALLOWED_EXTENSIONS', ['.mp3', '.wav', '.flac']))
MPV_CMD = cfg.get('MPV_CMD') or cfg.get('MPV') or ''

# 播放列表 & 自动播放
PLAYLIST = []            # 存储相对路径（相对 MUSIC_DIR）
CURRENT_INDEX = -1
_AUTO_THREAD = None
_STOP_FLAG = False
_REQ_ID = 0
CURRENT_META = {}  # 仅内存保存当前播放信息，不写入 settings.json

# =========== 文件树 / 安全路径 ===========
def safe_path(rel: str):
	base = os.path.abspath(MUSIC_DIR)
	target = os.path.abspath(os.path.join(base, rel))
	if not target.startswith(base):
		raise ValueError('非法路径')
	if not os.path.exists(target):
		raise ValueError('不存在的文件')
	return target

def gather_tracks(root):
	tracks = []
	for dp, _, files in os.walk(root):
		for f in files:
			ext = os.path.splitext(f)[1].lower()
			if ext in ALLOWED:
				tracks.append(os.path.abspath(os.path.join(dp, f)))
	return tracks

def build_tree():
	abs_root = os.path.abspath(MUSIC_DIR)
	def walk(path):
		rel = os.path.relpath(path, abs_root).replace('\\', '/')
		node = { 'name': os.path.basename(path) or '根目录', 'rel': '' if rel == '.' else rel, 'dirs': [], 'files': [] }
		try:
			for name in sorted(os.listdir(path), key=str.lower):
				full = os.path.join(path, name)
				if os.path.isdir(full):
					node['dirs'].append(walk(full))
				else:
					ext = os.path.splitext(name)[1].lower()
					if ext in ALLOWED:
						rp = os.path.relpath(full, abs_root).replace('\\','/')
						node['files'].append({'name': name, 'rel': rp})
		except Exception:
			pass
		return node
	return walk(abs_root)

# =========== MPV 启动 & IPC ===========
def _wait_pipe(timeout=6.0):
	end = time.time() + timeout
	while time.time() < end:
		try:
			with open(PIPE_NAME, 'wb') as _: return True
		except Exception: time.sleep(0.15)
	return False

def ensure_mpv():
	if not MPV_CMD:
		print('[WARN] 未配置 MPV_CMD')
		return False
	# 简单探测：尝试写入
	try:
		with open(PIPE_NAME, 'wb') as _: return True
	except Exception:
		pass
	try:
		subprocess.Popen(MPV_CMD, shell=True)
		return _wait_pipe()
	except Exception as e:
		print('[ERROR] 启动 mpv 失败:', e)
		return False

def mpv_command(cmd_list):
	try:
		with open(PIPE_NAME, 'wb') as w:
			w.write((json.dumps({'command': cmd_list})+'\n').encode('utf-8'))
	except Exception as e:
		raise RuntimeError(f'MPV 管道写入失败: {e}')

def mpv_request(payload: dict):
	# 简单同步请求/响应
	with open(PIPE_NAME, 'r+b', 0) as f:
		f.write((json.dumps(payload)+'\n').encode('utf-8'))
		f.flush()
		while True:
			line = f.readline()
			if not line:
				break
			try:
				obj = json.loads(line.decode('utf-8','ignore'))
			except Exception:
				continue
			if obj.get('request_id') == payload.get('request_id'):
				return obj
	return None

def mpv_get(prop: str):
	global _REQ_ID
	_REQ_ID += 1
	req = {"command":["get_property", prop], "request_id": _REQ_ID}
	resp = mpv_request(req)
	if not resp:
		return None
	return resp.get('data')

def mpv_set(prop: str, value):
	try:
		mpv_command(['set_property', prop, value])
		return True
	except Exception:
		return False

def _build_playlist():
	abs_root = os.path.abspath(MUSIC_DIR)
	tracks = []
	for dp, _, files in os.walk(abs_root):
		for f in files:
			ext = os.path.splitext(f)[1].lower()
			if ext in ALLOWED:
				rel = os.path.relpath(os.path.join(dp,f), abs_root).replace('\\','/')
				tracks.append(rel)
	tracks.sort(key=str.lower)
	return tracks

def _play_index(idx: int):
	global CURRENT_INDEX, CURRENT_META
	if idx < 0 or idx >= len(PLAYLIST):
		return False
	rel = PLAYLIST[idx]
	abs_file = safe_path(rel)
	mpv_command(['loadfile', abs_file, 'replace'])
	CURRENT_INDEX = idx
	CURRENT_META = {'abs_path': abs_file, 'rel': rel, 'index': idx, 'ts': int(time.time())}
	return True

def _next_track():
	if CURRENT_INDEX < 0:
		return False
	nxt = CURRENT_INDEX + 1
	if nxt >= len(PLAYLIST):
		return False
	return _play_index(nxt)

def _prev_track():
	if CURRENT_INDEX < 0:
		return False
	prv = CURRENT_INDEX - 1
	if prv < 0:
		return False
	return _play_index(prv)

def _auto_loop():
	while not _STOP_FLAG:
		if CURRENT_INDEX >= 0:
			try:
				eof = mpv_get('eof-reached')
				if eof is True:
					if not _next_track():
						time.sleep(1.2)
						continue
			except Exception:
				pass
		time.sleep(0.9)

def _ensure_auto_thread():
	global _AUTO_THREAD
	if _AUTO_THREAD and _AUTO_THREAD.is_alive():
		return
	_AUTO_THREAD = threading.Thread(target=_auto_loop, daemon=True)
	_AUTO_THREAD.start()

# =========== 路由 ===========
@APP.route('/')
def index():
	tree = build_tree()
	return render_template('index.html', tree=tree, music_dir=MUSIC_DIR)

@APP.route('/play', methods=['POST'])
def play_route():
	from flask import request
	rel = (request.form.get('path') or '').strip()
	if not rel:
		return jsonify({'status':'ERROR','error':'缺少 path'}), 400
	try:
		if not ensure_mpv():
			return jsonify({'status':'ERROR','error':'mpv 启动失败'}), 400
		global PLAYLIST, CURRENT_INDEX
		if not PLAYLIST or rel not in PLAYLIST:
			PLAYLIST = _build_playlist()
		if rel not in PLAYLIST:
			return jsonify({'status':'ERROR','error':'文件不在列表'}), 400
		idx = PLAYLIST.index(rel)
		if not _play_index(idx):
			return jsonify({'status':'ERROR','error':'播放失败'}), 400
		_ensure_auto_thread()
		return jsonify({'status':'OK','rel':rel,'index':idx,'total':len(PLAYLIST)})
	except Exception as e:
		return jsonify({'status':'ERROR','error':str(e)}), 400

@APP.route('/tree')
def tree_json():
	return jsonify({'status':'OK','tree':build_tree()})

@APP.route('/next', methods=['POST'])
def api_next():
	if not ensure_mpv():
		return jsonify({'status':'ERROR','error':'mpv 未就绪'}), 400
	if _next_track():
		return jsonify({'status':'OK','rel': PLAYLIST[CURRENT_INDEX], 'index': CURRENT_INDEX, 'total': len(PLAYLIST)})
	return jsonify({'status':'ERROR','error':'没有下一首'}), 400

@APP.route('/prev', methods=['POST'])
def api_prev():
	if not ensure_mpv():
		return jsonify({'status':'ERROR','error':'mpv 未就绪'}), 400
	if _prev_track():
		return jsonify({'status':'OK','rel': PLAYLIST[CURRENT_INDEX], 'index': CURRENT_INDEX, 'total': len(PLAYLIST)})
	return jsonify({'status':'ERROR','error':'没有上一首'}), 400

@APP.route('/status')
def api_status():
	"""返回当前播放状态（仅内存），所有客户端轮询实现共享可见性。"""
	playing = CURRENT_META if CURRENT_META else {}
	mpv_info = {}
	# 仅在 mpv 管道可用时尝试获取实时播放属性
	try:
		with open(PIPE_NAME, 'wb') as _:
			try:
				pos = mpv_get('time-pos')
				dur = mpv_get('duration')
				paused = mpv_get('pause')
				vol = mpv_get('volume')
				mpv_info = {
					'time': pos,
					'duration': dur,
					'paused': paused,
					'volume': vol
				}
			except Exception:
				pass
	except Exception:
		pass
	return jsonify({'status':'OK','playing': playing, 'mpv': mpv_info})

@APP.route('/volume', methods=['POST'])
def api_volume():
	from flask import request
	# form: value 可选(0-100). 不提供则返回当前音量
	if not ensure_mpv():
		return jsonify({'status':'ERROR','error':'mpv 未就绪'}), 400
	val = request.form.get('value')
	if val is None or val == '':
		cur = mpv_get('volume')
		return jsonify({'status':'OK','volume': cur})
	try:
		f = float(val)
	except ValueError:
		return jsonify({'status':'ERROR','error':'数值非法'}), 400
	if f < 0: f = 0
	if f > 130: f = 130
	if not mpv_set('volume', f):
		return jsonify({'status':'ERROR','error':'设置失败'}), 400
	return jsonify({'status':'OK','volume': f})

if __name__ == '__main__':
	APP.run(host=cfg.get('FLASK_HOST','0.0.0.0'), port=cfg.get('FLASK_PORT',8000), debug=cfg.get('DEBUG',False))

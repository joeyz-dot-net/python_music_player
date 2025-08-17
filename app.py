import os, sys, json, threading, time, subprocess, configparser
from flask import Flask, render_template, jsonify

APP = Flask(__name__, template_folder='.')

#############################################
# 配置: settings.ini (仅使用 INI, 已彻底移除 settings.json 支持)
#############################################
_LOCK = threading.RLock()

DEFAULT_CFG = {
	'MUSIC_DIR': 'Z:',
	'ALLOWED_EXTENSIONS': '.mp3,.wav,.flac',  # INI 中用逗号/分号分隔
	'FLASK_HOST': '0.0.0.0',
	'FLASK_PORT': '9000',
	'DEBUG': 'true',
	'MPV_CMD': r'c:\mpv\mpv.exe --input-ipc-server=\\.\pipe\mpv-pipe --idle=yes --force-window=no'
}

def _ini_path():
	if getattr(sys, 'frozen', False):
		return os.path.join(os.path.dirname(sys.executable), 'settings.ini')
	return os.path.join(os.path.dirname(__file__), 'settings.ini')

def _ensure_ini_exists():
	ini_path = _ini_path()
	if os.path.exists(ini_path):
		return
	cp = configparser.ConfigParser()
	cp['app'] = DEFAULT_CFG.copy()
	with open(ini_path,'w',encoding='utf-8') as w:
		cp.write(w)
	print('[INFO] 已生成默认 settings.ini')

def _read_ini_locked():
	ini_path = _ini_path()
	cp = configparser.ConfigParser()
	read_ok = cp.read(ini_path, encoding='utf-8')
	if not read_ok:
		return DEFAULT_CFG.copy()
	if 'app' not in cp:
		return DEFAULT_CFG.copy()
	raw = DEFAULT_CFG.copy()
	for k,v in cp['app'].items():
		raw[k.upper()] = v
	return raw

def load_settings():
	with _LOCK:
		return json.loads(json.dumps(_read_ini_locked()))  # 深拷贝

def update_settings(patch: dict):
	with _LOCK:
		cfg = _read_ini_locked()
		for k,v in patch.items():
			cfg[k.upper()] = v
		# 写回
		cp = configparser.ConfigParser()
		cp['app'] = {}
		for k,v in cfg.items():
			if k == 'ALLOWED_EXTENSIONS':
				if isinstance(v, (list,tuple,set)):
					cp['app'][k] = ','.join(sorted(v))
				else:
					cp['app'][k] = str(v)
			else:
				cp['app'][k] = str(v)
		ini_path = _ini_path()
		tmp = ini_path + '.tmp'
		with open(tmp,'w',encoding='utf-8') as w:
			cp.write(w)
		os.replace(tmp, ini_path)
		return cfg

_ensure_ini_exists()
cfg = load_settings()
#############################################

# 下面使用 cfg 不变
MUSIC_DIR = cfg.get('MUSIC_DIR', 'Z:')
if len(MUSIC_DIR) == 2 and MUSIC_DIR[1] == ':' and MUSIC_DIR[0].isalpha():
    MUSIC_DIR += '\\'
MUSIC_DIR = os.path.abspath(MUSIC_DIR)
_ext_raw = cfg.get('ALLOWED_EXTENSIONS', '.mp3,.wav,.flac')
if isinstance(_ext_raw, str):
	parts = [e.strip() for e in _ext_raw.replace(';',',').split(',') if e.strip()]
else:
	parts = list(_ext_raw)
ALLOWED = set([e if e.startswith('.') else '.'+e for e in parts])
MPV_CMD = cfg.get('MPV_CMD') or cfg.get('MPV') or ''

def _extract_pipe_name(cmd: str, fallback: str = r'\\.\\pipe\\mpv-pipe') -> str:
	"""从 MPV_CMD 中解析 --input-ipc-server 值; 支持两种形式:
	1) --input-ipc-server=\\.\\pipe\\mpv-pipe
	2) --input-ipc-server \\.\\pipe\\mpv-pipe
	若解析失败返回 fallback.
	"""
	if not cmd:
		return fallback
	parts = cmd.split()
	for i,p in enumerate(parts):
		if p.startswith('--input-ipc-server='):
			val = p.split('=',1)[1].strip().strip('"')
			return val or fallback
		if p == '--input-ipc-server' and i+1 < len(parts):
			val = parts[i+1].strip().strip('"')
			if val and not val.startswith('--'):
				return val
	return fallback

# 兼容: 若 settings 仍含 PIPE_NAME 则优先; 否则从 MPV_CMD 解析
PIPE_NAME = cfg.get('PIPE_NAME') or _extract_pipe_name(MPV_CMD)

def mpv_pipe_exists(path: str = None) -> bool:
	p = path or PIPE_NAME
	try:
		with open(p, 'wb'):
			return True
	except Exception:
		return False

# 播放列表 & 自动播放
PLAYLIST = []            # 存储相对路径（相对 MUSIC_DIR）
CURRENT_INDEX = -1
_AUTO_THREAD = None
_STOP_FLAG = False
_REQ_ID = 0
CURRENT_META = {}  # 仅内存保存当前播放信息，不写入 settings.json
SHUFFLE = False

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
	global PIPE_NAME
	# 每次调用重新解析，允许运行期间修改 MPV_CMD 并热加载（若外部修改变量并重载模块则生效）
	PIPE_NAME = _extract_pipe_name(MPV_CMD) if not cfg.get('PIPE_NAME') else cfg.get('PIPE_NAME')
	if not MPV_CMD:
		print('[WARN] 未配置 MPV_CMD')
		return False
	if mpv_pipe_exists():
		return True
	print(f'[INFO] 尝试启动 mpv: {MPV_CMD}')
	try:
		subprocess.Popen(MPV_CMD, shell=True)
	except Exception as e:
		print('[ERROR] 启动 mpv 进程失败:', e)
		return False
	ready = _wait_pipe()
	if not ready:
		print('[ERROR] 等待 mpv 管道超时: ', PIPE_NAME)
	return ready

def mpv_command(cmd_list):
	# 写命令，失败时自动尝试启动一次再重试
	def _write():
		with open(PIPE_NAME, 'wb') as w:
			w.write((json.dumps({'command': cmd_list})+'\n').encode('utf-8'))
	try:
		_write()
	except Exception as e:
		print(f'[WARN] 首次写入失败: {e}. 尝试 ensure_mpv 后重试...')
		if ensure_mpv():
			try:
				_write()
				return
			except Exception as e2:
				raise RuntimeError(f'MPV 管道写入失败(重试): {e2}')
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

def _ensure_playlist(force: bool = False):
	"""确保内存 PLAYLIST 存在; force=True 时强制重建."""
	global PLAYLIST
	if force or not PLAYLIST:
		PLAYLIST = _build_playlist()
	return PLAYLIST

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
	import random
	if CURRENT_INDEX < 0:
		return False
	if SHUFFLE and len(PLAYLIST) > 1:
		# 随机选择一个不同的索引
		choices = list(range(len(PLAYLIST)))
		try:
			choices.remove(CURRENT_INDEX)
		except ValueError:
			pass
		if not choices:
			return False
		return _play_index(random.choice(choices))
	nxt = CURRENT_INDEX + 1
	if nxt >= len(PLAYLIST):
		return False
	return _play_index(nxt)

def _prev_track():
	import random
	if CURRENT_INDEX < 0:
		return False
	if SHUFFLE and len(PLAYLIST) > 1:
		choices = list(range(len(PLAYLIST)))
		try:
			choices.remove(CURRENT_INDEX)
		except ValueError:
			pass
		if not choices:
			return False
		return _play_index(random.choice(choices))
	prv = CURRENT_INDEX - 1
	if prv < 0:
		return False
	return _play_index(prv)

def _auto_loop():
	print('[INFO] 自动播放线程已启动')
	while not _STOP_FLAG:
		print('[DEBUG] 自动播放检查...')
		if CURRENT_INDEX < 0:
			# 没有正在播放的，尝试自动加载并播第一首
			_ensure_playlist()
			if PLAYLIST:
				_play_index(0)
				time.sleep(1.0)
				continue
		try:
			# 侦测曲目结束: 优先 eof-reached, 其次 time-pos≈duration, 再次 idle-active
			ended = False
			pos = mpv_get('time-pos')
			dur = mpv_get('duration')
			eof = mpv_get('eof-reached')  # 可能为 None
			if eof is True:
				ended = True
			elif isinstance(pos,(int,float)) and isinstance(dur,(int,float)) and dur>0 and (dur - pos) <= 0.3:
				ended = True
			else:
				idle = mpv_get('idle-active')
				if idle is True and (pos is None or (isinstance(pos,(int,float)) and pos==0)):
					ended = True
			if ended:
				print('[INFO] 当前曲目已结束，尝试播放下一首...')
				if not _next_track():
					# 到末尾，等待再尝试
					time.sleep(10)
					continue
		except Exception:
			pass
		time.sleep(10)

def _ensure_auto_thread():
	global _AUTO_THREAD
	if _AUTO_THREAD and _AUTO_THREAD.is_alive():
		print('[INFO] 自动播放线程已存在')
		return
	_AUTO_THREAD = threading.Thread(target=_auto_loop, daemon=True)
	_AUTO_THREAD.start()

# =========== 路由 ===========
@APP.route('/')
def index():
	tree = build_tree()
	#_AUTO_THREAD = True
	_ensure_auto_thread()
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

@APP.route('/shuffle', methods=['POST'])
def api_shuffle():
	"""切换随机播放模式."""
	global SHUFFLE
	SHUFFLE = not SHUFFLE
	return jsonify({'status':'OK','shuffle': SHUFFLE})

@APP.route('/playlist')
def api_playlist():
	"""返回当前播放列表。

	参数:
	  rebuild=1  强制重建扫描
	  offset, limit  分页 (可选)
	"""
	from flask import request
	force = request.args.get('rebuild') == '1'
	plist = _ensure_playlist(force)
	offset = int(request.args.get('offset', '0') or 0)
	limit = request.args.get('limit')
	if limit is not None:
		try:
			limit_i = max(0, int(limit))
		except ValueError:
			limit_i = 0
	else:
		limit_i = 0
	data = plist
	if offset < 0: offset = 0
	if limit_i > 0:
		data = plist[offset: offset+limit_i]
	return jsonify({
		'status': 'OK',
		'total': len(plist),
		'index': CURRENT_INDEX,
		'current': CURRENT_META.get('rel') if CURRENT_META else None,
		'offset': offset,
		'limit': limit_i or None,
		'playlist': data
	})

@APP.route('/debug/mpv')
def api_debug_mpv():
	info = {
		'MPV_CMD': MPV_CMD,
		'PIPE_NAME': PIPE_NAME,
		'pipe_exists': mpv_pipe_exists(),
		'playlist_len': len(PLAYLIST),
		'current_index': CURRENT_INDEX,
		'shuffle': 'SHUFFLE' in globals() and globals().get('SHUFFLE')
	}
	return jsonify({'status':'OK','info': info})

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

print("Build marker:", time.time())

if __name__ == '__main__':
	APP.run(host=cfg.get('FLASK_HOST','0.0.0.0'), port=cfg.get('FLASK_PORT',8000), debug=cfg.get('DEBUG',False))

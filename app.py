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
		abs_file = safe_path(rel)
		mpv_command(['loadfile', abs_file, 'replace'])
		update_settings({'current_playing': {'abs_path': abs_file, 'rel': rel, 'ts': int(time.time())}})
		return jsonify({'status':'OK','rel':rel})
	except Exception as e:
		return jsonify({'status':'ERROR','error':str(e)}), 400

@APP.route('/tree')
def tree_json():
	return jsonify({'status':'OK','tree':build_tree()})

if __name__ == '__main__':
	APP.run(host=cfg.get('FLASK_HOST','0.0.0.0'), port=cfg.get('FLASK_PORT',8000), debug=cfg.get('DEBUG',False))

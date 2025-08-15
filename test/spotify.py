"""简单的 Spotify API 测试用例

运行方式（需已填写 settings.json 中的 SPOTIFY_CLIENT_ID/SECRET）:
  python -m test.spotify

测试内容:
 1. 获取 token
 2. 搜索歌手，获取头像 URL
 3. 搜索歌曲专辑封面

注意：
 - 依赖公网访问，失败可能是网络或凭证问题。
 - 不使用 pytest/unittest 框架，直接脚本式断言；若需要集成可再改。
"""

import json, os, sys, time, textwrap, argparse
from typing import Optional
import requests

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, 'settings.json')

def load_settings():
	try:
		with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
			return json.load(f)
	except Exception as e:
		print('[ERR] 读取 settings.json 失败:', e)
		return {}

def get_token(client_id: str, client_secret: str, debug: bool=False) -> Optional[str]:
	"""获取 Spotify token；debug 模式打印详细请求/响应。"""
	url = 'https://accounts.spotify.com/api/token'
	try:
		r = requests.post(url, data={'grant_type': 'client_credentials'}, auth=(client_id, client_secret), timeout=10)
	except requests.exceptions.RequestException as e:
		print('[ERR] 网络异常获取 token 失败:', e)
		return None
	except Exception as e:
		print('[ERR] 未知异常获取 token 失败:', e)
		return None

	if debug:
		print(f'[DEBUG] Token HTTP status: {r.status_code}')
		# 不打印 Authorization 头
		safe_headers = {k: v for k,v in r.request.headers.items() if k.lower() != 'authorization'}
		print('[DEBUG] Request headers:', safe_headers)
		print('[DEBUG] Response headers:', dict(r.headers))
	if r.status_code != 200:
		print('[ERR] Token 请求失败 status=', r.status_code)
		txt = r.text
		if debug:
			print('[DEBUG] 原始响应正文:', txt)
		print('[ERR] 响应正文截断:', txt[:500])
		return None
	try:
		j = r.json()
	except Exception:
		print('[ERR] 响应不是合法 JSON:', r.text[:200])
		return None
	token = j.get('access_token')
	if not token:
		print('[ERR] 未在响应中找到 access_token 字段，完整 JSON:')
		print(json.dumps(j, ensure_ascii=False, indent=2))
	else:
		if debug:
			print('[DEBUG] token length =', len(token))
	return token

def search_artist(name: str, token: str, debug: bool=False):
	url = 'https://api.spotify.com/v1/search'
	r = requests.get(url, params={'q': name, 'type': 'artist', 'limit': 1}, headers={'Authorization': f'Bearer {token}'}, timeout=8)
	if debug:
		print(f'[DEBUG] Artist search status={r.status_code} url={r.url}')
	j = r.json()
	items = j.get('artists', {}).get('items', [])
	if items:
		a = items[0]
		return {
			'name': a.get('name'),
			'image': (a.get('images') or [{}])[0].get('url'),
			'genres': a.get('genres', []),
			'popularity': a.get('popularity'),
		}
	return {}

def search_track_cover(artist: str, track: str, token: str, debug: bool=False) -> str:
	url = 'https://api.spotify.com/v1/search'
	q1 = f'track:{track} artist:{artist}' if artist else f'track:{track}'
	for q in [q1, f'track:{track}', track]:
		r = requests.get(url, params={'q': q, 'type': 'track', 'limit': 1}, headers={'Authorization': f'Bearer {token}'}, timeout=8)
		if debug:
			print(f'[DEBUG] Track search q="{q}" status={r.status_code} url={r.url}')
		j = r.json()
		items = j.get('tracks', {}).get('items', [])
		if items and items[0].get('album', {}).get('images'):
			return items[0]['album']['images'][0]['url']
	return ''

def assert_true(cond: bool, msg: str):
	if not cond:
		raise AssertionError(msg)

def main():
	parser = argparse.ArgumentParser(description='Spotify API 调试脚本')
	parser.add_argument('--debug', action='store_true', help='打印调试信息')
	parser.add_argument('--artist', default='Taylor Swift', help='测试歌手名')
	parser.add_argument('--track', default='Love Story', help='测试歌曲名')
	args = parser.parse_args()
	debug = args.debug

	cfg = load_settings()
	cid = cfg.get('SPOTIFY_CLIENT_ID')
	csec = cfg.get('SPOTIFY_CLIENT_SECRET')
	if not cid or not csec:
		print('[WARN] 未在 settings.json 中找到 SPOTIFY_CLIENT_ID / SECRET，跳过测试')
		return

	print('== 0. 凭证形态 ==')
	def mask(v):
		return f'{v[:4]}***{v[-4:]} (len={len(v)})' if v else '<空>'
	print('Client ID   :', mask(cid))
	if cid and cid.islower():
		print('[WARN] Client ID 全小写，Spotify ID 通常包含大小写字母 + 数字，可能复制错误。')
	print('Client Secret:', mask(csec))

	print('\n== 1. 获取 Token ==')
	token = get_token(cid, csec, debug=debug)
	if not (token and len(token) > 20):
		print('\n排查建议:')
		print(' 1. 确认 settings.json 中 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET 正确')
		print(' 2. 这些凭证来自 https://developer.spotify.com/dashboard 创建的应用 (Dashboard -> App -> Client ID/Secret)')
		print(' 3. 若在国内/受限网络环境，需代理；可测试: curl https://accounts.spotify.com/api/token')
		print(' 4. 确认系统时间准确 (OAuth 对时间漂移敏感)')
		print(' 5. 过多失败请求可能被限速，稍等再试')
		raise AssertionError('Token 获取失败')
	print('Token OK:', token[:25] + '...')
	artist_name = args.artist
	print(f'\n== 2. 搜索歌手: {artist_name} ==')
	ainfo = search_artist(artist_name, token, debug=debug)
	assert_true(ainfo.get('name'), '未获取到歌手名称')
	print('歌手信息:', json.dumps({k: ainfo[k] for k in ['name','image','genres','popularity'] if k in ainfo}, ensure_ascii=False, indent=2))

	track_title = args.track
	print(f'\n== 3. 搜索歌曲封面: {artist_name} - {track_title} ==')
	cover = search_track_cover(artist_name, track_title, token, debug=debug)
	assert_true(cover.startswith('http'), '未获取到专辑封面 URL')
	print('封面 URL:', cover)

	print('\n全部通过 ✅')

if __name__ == '__main__':
	try:
		main()
	except AssertionError as e:
		print('\n[FAIL]', e)
		sys.exit(1)
	except Exception as e:
		print('\n[ERROR] 未处理异常:', e)
		sys.exit(2)

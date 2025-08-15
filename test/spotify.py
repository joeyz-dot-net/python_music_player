"""简单 Spotify API 调用测试

步骤:
 1. 从 settings.json 读取 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET
 2. 获取 client credentials access_token
 3. 调用搜索接口 /v1/search (示例: Love Story Taylor Swift)
 4. 拉取第一条 track 详情 /v1/tracks/{id}

运行:
  python test/spotify.py
可修改 QUERY 常量测试其它关键词。
"""

import requests, base64, json, os, sys

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
SETTINGS = os.path.join(BASE_DIR, 'settings.json')
QUERY = "Love Story Taylor Swift"  # 修改测试关键词

def load_cfg():
    try:
        with open(SETTINGS, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print('[ERR] 读取 settings.json 失败:', e)
        return {}

def get_token(cid: str, sec: str):
    pair = f"{cid}:{sec}".encode()
    b64 = base64.b64encode(pair).decode()
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": f"Basic {b64}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={"grant_type": "client_credentials"},
        timeout=12
    )
    if r.status_code != 200:
        print('[ERR] 获取 token 失败:', r.status_code, r.text[:400])
        return None
    try:
        return r.json().get('access_token')
    except Exception:
        print('[ERR] token JSON 解析失败')
        return None

def search_track(q: str, token: str):
    url = 'https://api.spotify.com/v1/search'
    r = requests.get(url, params={'q': q, 'type': 'track', 'limit': 1}, headers={'Authorization': f'Bearer {token}'}, timeout=10)
    if r.status_code != 200:
        print('[ERR] 搜索失败:', r.status_code, r.text[:200])
        return None
    try:
        items = r.json().get('tracks', {}).get('items', [])
        return items[0] if items else None
    except Exception:
        return None

def track_detail(track_id: str, token: str):
    url = f'https://api.spotify.com/v1/tracks/{track_id}'
    r = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=10)
    if r.status_code != 200:
        print('[ERR] 获取 track 详情失败:', r.status_code, r.text[:200])
        return None
    try:
        return r.json()
    except Exception:
        return None

def main():
    cfg = load_cfg()
    cid = cfg.get('SPOTIFY_CLIENT_ID')
    sec = cfg.get('SPOTIFY_CLIENT_SECRET')
    if not cid or not sec:
        print('[FAIL] settings.json 缺少 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET')
        sys.exit(1)

    print('== 获取 Token ==')
    token = get_token(cid, sec)
    if not token:
        print('[FAIL] 无法获取 token')
        sys.exit(1)
    print('Token OK (len):', len(token))

    print('\n== 搜索 Track ==')
    print('Query:', QUERY)
    item = search_track(QUERY, token)
    if not item:
        print('[FAIL] 未找到歌曲')
        sys.exit(1)
    track_id = item.get('id')
    print('Match:', item.get('name'), '-', ', '.join(a.get('name') for a in item.get('artists', [])))
    print('Track ID:', track_id)

    print('\n== Track 详情 ==')
    detail = track_detail(track_id, token) if track_id else None
    if not detail:
        print('[FAIL] 获取详情失败')
        sys.exit(1)
    summary = {
        'name': detail.get('name'),
        'album': detail.get('album', {}).get('name'),
        'release_date': detail.get('album', {}).get('release_date'),
        'duration_ms': detail.get('duration_ms'),
        'popularity': detail.get('popularity'),
        'explicit': detail.get('explicit'),
        'preview_url': detail.get('preview_url')
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not summary['name']:
        print('[FAIL] 歌曲名称为空')
        sys.exit(1)
    print('\n✅ 全部通过')
    sys.exit(0)

if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print('[ERROR] 未处理异常:', e)
        sys.exit(2)
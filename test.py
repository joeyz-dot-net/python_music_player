import json
import time
import threading

class MPVStatusClient:
    def __init__(self, pipe_name=r"\\.\pipe\mpv-pipe"):
        self.pipe_name = pipe_name
        self.f = None
        self.lock = threading.Lock()

    def connect(self):
        try:
            self.f = open(self.pipe_name, "r+b", buffering=0)
            return True
        except:
            return False

    def close(self):
        if self.f:
            self.f.close()
            self.f = None

    def request(self, command, timeout=1.0):
        with self.lock:
            try:
                payload = json.dumps({"command": command}) + "\n"
                self.f.write(payload.encode("utf-8"))
                return self._read_response(timeout)
            except:
                return None

    def _read_response(self, timeout):
        result = {"line": None}
        def reader():
            try:
                result["line"] = self.f.readline()
            except:
                pass
        t = threading.Thread(target=reader)
        t.start()
        t.join(timeout)
        if not result["line"]:
            return None
        try:
            return json.loads(result["line"].decode("utf-8"))
        except:
            return None

    def get_status(self):
        if not self.f:
            return {"error": "pipe not connected"}
        data = {}
        for key in ["media-title", "playback-time", "duration", "idle-active"]:
            resp = self.request(["get_property", key])
            data[key] = resp.get("data") if resp else None
        return data


client = MPVStatusClient()
if client.connect():
    status = client.get_status()
    print("当前播放状态:", status)
    client.close()
else:
    print("无法连接管道。请确认 MPV 正在运行并启用了 IPC。")
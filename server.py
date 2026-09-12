# -*- coding: utf-8 -*-
"""音频字幕播放器 - 本地伴生服务
零依赖（仅 Python 标准库）。启动后：
  1. 自动打开浏览器访问播放器页面
  2. 播放列表保存在本文件同目录的 playlist.json（只记录音频/字幕的原始路径，不复制文件）
  3. /api/browse 浏览磁盘目录，供页面选择文件
  4. /media?path=<绝对路径> 直接播放磁盘任意位置的音频（支持 Range 请求）
  5. /api/open 在资源管理器中定位音频文件所在目录
用法：双击同目录的「启动播放器.bat」，或 python server.py
测试模式：环境变量 PLAYER_HEADLESS=1 时不打开浏览器、不调 os.startfile；
         环境变量 PLAYER_PORT 可覆盖默认端口 8765。
"""
import glob
import json
import os
import re
import string
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

PORT = int(os.environ.get("PLAYER_PORT") or 8765)
ROOT = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(ROOT, "媒体库")  # 旧版本复制文件的目录，仅为兼容旧列表保留
PLAYLIST = os.path.join(ROOT, "playlist.json")
LEGACY_PLAYLIST = os.path.join(MEDIA_DIR, "playlist.json")
HEADLESS = os.environ.get("PLAYER_HEADLESS") == "1"
_opens = []  # HEADLESS 模式下记录被请求打开的目录/文件，供自动化测试断言

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma",
              ".mp4", ".mkv", ".webm"}
SUB_EXTS = {".srt", ".vtt", ".lrc", ".txt"}

try:
    os.makedirs(MEDIA_DIR, exist_ok=True)
except Exception as e:
    sys.stderr.write("cannot create media dir: %s\n" % e)

HTML_CANDIDATES = [f for f in glob.glob(os.path.join(ROOT, "*.html"))]
HTML_FILE = None
for f in HTML_CANDIDATES:
    if "音频字幕播放器" in os.path.basename(f):
        HTML_FILE = f
        break
if not HTML_FILE and HTML_CANDIDATES:
    HTML_FILE = sorted(HTML_CANDIDATES, key=os.path.getmtime)[-1]

MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".opus": "audio/opus", ".wma": "audio/x-ms-wma",
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".txt": "text/plain; charset=utf-8", ".srt": "text/plain; charset=utf-8",
    ".vtt": "text/vtt", ".lrc": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript", ".css": "text/css",
    ".json": "application/json",
}


def now_ms():
    return int(time.time() * 1000)


def safe_name(name):
    """去掉路径分隔与 Windows 非法字符，防目录穿越"""
    name = os.path.basename(str(name).replace("\\", "/"))
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip()
    return name or "file"


def load_playlist():
    try:
        with open(PLAYLIST, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_playlist(items):
    atomic_write(PLAYLIST, json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8"))


def clean_bookmarks(data):
    """从请求里取书签数组，过滤非法项；缺字段返回 None（表示本次请求不带书签，不覆盖）"""
    if "bookmarks" not in data or data["bookmarks"] is None:
        return None
    out = []
    for b in data["bookmarks"] or []:
        if isinstance(b, dict) and isinstance(b.get("t"), (int, float)) and b["t"] >= 0:
            out.append({"t": round(float(b["t"]), 3), "text": str(b.get("text") or "")[:200]})
    out.sort(key=lambda x: x["t"])
    return out


def atomic_write(fp, data):
    """优先临时文件+原子替换；沙箱/杀软拦截 rename 时退化为直接写入"""
    tmp = fp + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        os.replace(tmp, fp)
        return
    except PermissionError:
        pass
    try:
        if os.path.isfile(fp):
            os.remove(fp)
    except Exception:
        pass
    try:
        os.replace(tmp, fp)
        return
    except Exception:
        pass
    with open(fp, "wb") as f:
        f.write(data)
    try:
        os.remove(tmp)
    except Exception:
        pass


def _norm(p):
    """路径归一化，用于列表条目匹配（Windows 下忽略大小写）"""
    p = os.path.normpath(os.path.abspath(p))
    return p.lower() if os.name == "nt" else p


def _migrate_playlist():
    """旧版 playlist.json 在媒体库文件夹里，迁移到 HTML 同目录"""
    if not os.path.isfile(PLAYLIST) and os.path.isfile(LEGACY_PLAYLIST):
        try:
            with open(LEGACY_PLAYLIST, "r", encoding="utf-8") as f:
                data = f.read()
            atomic_write(PLAYLIST, data.encode("utf-8"))
        except Exception:
            pass


_migrate_playlist()


def _looks_zero_padded(data):
    """尾部 64 字节全零 = 疑似被文件层零填充（正常 HTML/字幕不会这样）"""
    n = min(64, len(data))
    return n > 0 and data[-n:] == b"\x00" * n


def read_file_robust(fp, attempts=4):
    """整体读取并校验：个别环境的文件层偶发把 40KB 之后读成零字节，
    多读几次，优先返回未被零填充且与上一次一致的内容。"""
    prev = None
    best = None
    for _ in range(attempts):
        with open(fp, "rb") as f:
            data = f.read()
        if not _looks_zero_padded(data):
            if prev is not None and data == prev:
                return data
            best = data
        prev = data
    return best if best is not None else (prev or b"")


def detect_encoding(raw):
    """识别字幕文件原编码，保存时按原编码写回（避免中文变乱码）"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gb18030"


def remove_media(name):
    for fn in (name, name + ".tmp"):
        fp = os.path.join(MEDIA_DIR, fn)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "SubPlayer/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，控制台保持干净

    # ---------- 工具 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _read_json(self):
        try:
            return json.loads(self._read_body().decode("utf-8"))
        except Exception:
            return None

    # ---------- 目录浏览（供页面从磁盘选择文件） ----------
    def _api_browse(self, parsed):
        qs = parse_qs(parsed.query)
        d = unquote((qs.get("dir") or [""])[0]).strip().strip('"')
        if not d or not os.path.isabs(d) or not os.path.isdir(d):
            d = os.path.expanduser("~")
        try:
            entries = os.listdir(d)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        dirs, audio, subs = [], [], []
        for name in entries:
            if name.startswith("."):
                continue
            full = os.path.join(d, name)
            try:
                if os.path.isdir(full):
                    dirs.append(name)
                    continue
            except OSError:
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in AUDIO_EXTS:
                audio.append(name)
            elif ext in SUB_EXTS:
                subs.append(name)
        dirs.sort(key=str.lower)
        audio.sort(key=str.lower)
        subs.sort(key=str.lower)
        parent = os.path.dirname(d.rstrip("\\/")) if len(d.rstrip("\\/")) > 3 else ""
        drives = []
        if not parent:  # 已在盘符根目录：提供盘符列表
            for letter in string.ascii_uppercase:
                drv = letter + ":\\"
                if os.path.exists(drv):
                    drives.append(drv)
        return self._json({"ok": True, "dir": d, "parent": parent,
                           "dirs": dirs, "audio": audio, "subs": subs, "drives": drives})

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/ping":
            return self._json({"ok": True, "mediaDir": MEDIA_DIR,
                               "html": os.path.basename(HTML_FILE) if HTML_FILE else ""})
        if path == "/api/list":
            items = load_playlist()
            items.sort(key=lambda x: -(x.get("updated") or 0))
            return self._json({"ok": True, "items": items})
        if path == "/api/opens":
            return self._json({"ok": True, "dirs": _opens})  # HEADLESS 测试用
        if path == "/api/browse":
            return self._api_browse(parsed)
        if path == "/media" or path.startswith("/media/"):
            qs = parse_qs(parsed.query)
            p = unquote((qs.get("path") or [""])[0]).strip()
            if p:
                # 按路径播放磁盘上的原始文件（只允许音频/字幕扩展名）
                ext = os.path.splitext(p)[1].lower()
                if not os.path.isabs(p) or ext not in (AUDIO_EXTS | SUB_EXTS):
                    return self._json({"ok": False, "error": "forbidden"}, 403)
                return self._serve_file(p)
            # 旧版兼容：媒体库文件夹内的复制文件
            return self._serve_file(os.path.join(MEDIA_DIR, safe_name(path[len("/media/"):])))
        if path == "/":
            if HTML_FILE:
                return self._serve_file(HTML_FILE)
            return self._json({"ok": False, "error": "no html found"}, 404)
        return self._serve_file(os.path.join(ROOT, safe_name(path.lstrip("/"))))

    def _serve_file(self, fp):
        if not os.path.isfile(fp):
            return self._json({"ok": False, "error": "not found"}, 404)
        ext = os.path.splitext(fp)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        start, end, partial = 0, size - 1, False
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                partial = True
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        length = end - start + 1
        # 小文件（页面/字幕）：整体读入内存并做零填充校验，规避读取抖动
        body = None
        if not partial and size <= 1048576:
            body = read_file_robust(fp)
            length = len(body)
            end = start + length - 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "GET":
            if body is not None:
                self.wfile.write(body)
                return
            with open(fp, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    # ---------- POST ----------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        if path == "/api/entry":
            data = self._read_json()
            if not data:
                return self._json({"ok": False, "error": "bad request"}, 400)
            apath = (data.get("audioPath") or "").strip()
            if not apath and not data.get("audio"):
                return self._json({"ok": False, "error": "no audio"}, 400)
            items = load_playlist()
            found = None
            for it in items:
                if apath:
                    if it.get("audioPath") and _norm(it["audioPath"]) == _norm(apath):
                        found = it
                        break
                elif it.get("audio") == data.get("audio"):
                    found = it
                    break
            if found is None:
                found = {"audioPath": apath} if apath else {"audio": safe_name(data["audio"])}
                items.append(found)
            if apath:
                found["audioPath"] = os.path.normpath(apath)  # 统一分隔符，避免混用 / 与 \
                found["subPath"] = os.path.normpath(data.get("subPath") or "").strip() if (data.get("subPath") or "").strip() else ""
            else:
                found["sub"] = data.get("sub") or ""
                if (data.get("subPath") or "").strip():
                    found["subPath"] = data["subPath"].strip()  # 旧条目配新字幕：也允许按路径
            found["position"] = float(data.get("position") or 0)
            found["updated"] = int(data.get("updated") or now_ms())
            bms = clean_bookmarks(data)
            if bms is not None:
                found["bookmarks"] = bms
            save_playlist(items)
            return self._json({"ok": True})

        if path == "/api/progress":
            data = self._read_json()
            if not data:
                return self._json({"ok": False, "error": "bad request"}, 400)
            apath = (data.get("audioPath") or "").strip()
            if not apath and not data.get("audio"):
                return self._json({"ok": False, "error": "no audio"}, 400)
            items = load_playlist()
            for it in items:
                if apath:
                    if it.get("audioPath") and _norm(it["audioPath"]) == _norm(apath):
                        break
                elif it.get("audio") == data.get("audio"):
                    break
            else:
                return self._json({"ok": False, "error": "not found"}, 404)
            it["position"] = float(data.get("position") or 0)
            it["updated"] = now_ms()
            bms = clean_bookmarks(data)
            if bms is not None:
                it["bookmarks"] = bms
            save_playlist(items)
            return self._json({"ok": True})

        if path == "/api/delete":
            data = self._read_json()
            if not data:
                return self._json({"ok": False, "error": "bad request"}, 400)
            apath = (data.get("audioPath") or "").strip()
            items = load_playlist()
            kept, removed = [], []
            for it in items:
                hit = ((apath and it.get("audioPath") and _norm(it["audioPath"]) == _norm(apath))
                       or (not apath and it.get("audio") == data.get("audio")))
                (removed if hit else kept).append(it)
            save_playlist(kept)
            # 只从列表移除条目；旧版媒体库复制的文件一并清理，磁盘原始文件永不删除
            for it in removed:
                if it.get("audio"):
                    remove_media(it.get("audio") or "")
                if it.get("sub"):
                    remove_media(it["sub"])
            return self._json({"ok": True, "removed": len(removed)})

        if path == "/api/save-sub":
            # 把页面里编辑过的字幕文本写回原文件（时间轴由页面保证不变）
            data = self._read_json()
            if not data or not isinstance(data.get("text"), str):
                return self._json({"ok": False, "error": "bad request"}, 400)
            sub_path = (data.get("subPath") or "").strip()
            if sub_path:
                fp = os.path.normpath(sub_path)
                if not os.path.isabs(fp) or os.path.splitext(fp)[1].lower() not in SUB_EXTS:
                    return self._json({"ok": False, "error": "forbidden"}, 403)
            elif (data.get("sub") or "").strip():
                # 旧版媒体库条目：字幕副本存在媒体库文件夹
                fp = os.path.join(MEDIA_DIR, safe_name(data["sub"]))
            else:
                return self._json({"ok": False, "error": "no subtitle file linked"}, 400)
            if not os.path.isfile(fp):
                return self._json({"ok": False, "error": "file not found: " + fp}, 404)
            try:
                with open(fp, "rb") as f:
                    raw = f.read()
                enc = detect_encoding(raw)
                bak = fp + ".bak"
                backup = ""
                if not os.path.exists(bak):  # 只保留第一份原始备份
                    with open(bak, "wb") as f:
                        f.write(raw)
                    backup = bak
                atomic_write(fp, data["text"].encode(enc, "replace"))
                return self._json({"ok": True, "path": fp, "encoding": enc,
                                   "backup": backup, "size": os.path.getsize(fp)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)

        if path == "/api/open":
            data = self._read_json() or {}
            target = (data.get("path") or "").strip()
            try:
                if target and os.path.isfile(target):
                    # 在资源管理器中定位文件
                    if HEADLESS:
                        _opens.append("select:" + target)
                    elif sys.platform.startswith("win"):
                        import subprocess
                        subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
                    elif sys.platform == "darwin":
                        import subprocess
                        subprocess.Popen(["open", "-R", target])
                    else:
                        subprocess_open(os.path.dirname(target))
                else:
                    d = target if target and os.path.isdir(target) else ROOT
                    if HEADLESS:
                        _opens.append(d)
                    elif hasattr(os, "startfile"):
                        os.startfile(d)
                    else:
                        subprocess_open(d)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)

        return self._json({"ok": False, "error": "unknown endpoint"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_HEAD(self):
        self.command = "HEAD"
        self.do_GET()


def subprocess_open(target):
    import subprocess
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", target])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


def _port_alive():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def main():
    if not HTML_FILE:
        sys.stderr.write("WARNING: no html file found next to server.py\n")
    url = "http://localhost:%d/" % PORT
    # Windows 下 SO_REUSEADDR 允许多个实例同时绑同一端口，请求会被随机分发，
    # 页面可能来自旧实例（内容可能是旧的/坏的）。已有实例时不再启动第二个。
    if _port_alive():
        print("===================================================")
        print(" 端口 %d 已有一个播放器服务在运行，不再启动第二个实例。" % PORT)
        print(" 若页面行为异常：关闭旧的命令行窗口，重新双击「启动播放器.bat」。")
        print(" URL      : " + url)
        print("===================================================")
        if not HEADLESS:
            webbrowser.open(url)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("===================================================")
    print(" Audio Sub Player server running")
    print(" URL      : " + url)
    print(" MediaDir : " + MEDIA_DIR)
    print("===================================================")
    if not HEADLESS:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

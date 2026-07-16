#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Приёмник аудио для скрытого диктофона-часов.

Поток:
  PWA (часы) → POST /upload  (тело = сырые байты аудио, метаданные в query)
            → ElevenLabs Scribe (расшифровка в текст)
            → Telegram (личный чат): текст + при желании само голосовое.

Чистый Python stdlib, без pip. Разворачивается на Render (free), поэтому
работает при выключенном Mac.

Переменные окружения (Render → Environment):
  ELEVENLABS_API_KEY   — ключ ElevenLabs (тот же, что в speech-to-text)
  TELEGRAM_BOT_TOKEN   — токен бота от @BotFather
  TELEGRAM_CHAT_ID     — id твоего личного чата с ботом (см. README)
  UPLOAD_SECRET        — общий секрет: PWA шлёт его в ?key=, чужие запросы отсекаются
  SEND_AUDIO           — "1" (по умолч.) слать в ТГ и само голосовое; "0" — только текст
  STT_LANGUAGE         — код языка для Scribe, напр. "rus"; по умолч. auto
"""

import os
import io
import json
import time
import uuid
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------- конфигурация -----------------------------
ON_RENDER = bool(os.environ.get("RENDER"))
HOST = os.environ.get("HOST", "0.0.0.0" if ON_RENDER else "127.0.0.1")
PORT = int(os.environ.get("PORT", "7862"))

ELEVEN_BASE = "https://api.elevenlabs.io"
STT_MODEL = "scribe_v1"

API_KEY = None  # заполняется в load_key()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
UPLOAD_SECRET = os.environ.get("UPLOAD_SECRET", "").strip()
SEND_AUDIO = os.environ.get("SEND_AUDIO", "1").strip() not in ("0", "false", "no", "")
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "").strip()  # "" = auto

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def load_key():
    """ELEVENLABS_API_KEY из окружения, либо из локального .env (для запуска на Mac)."""
    global API_KEY
    if os.environ.get("ELEVENLABS_API_KEY"):
        API_KEY = os.environ["ELEVENLABS_API_KEY"].strip()
        return API_KEY
    for p in (os.path.join(os.path.dirname(__file__), ".env"),
              os.path.join(os.path.dirname(__file__), "..", "..", "speech-to-text", ".env")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass
    API_KEY = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    return API_KEY


# ----------------------------- ElevenLabs Scribe -----------------------------
def build_multipart(fields, file_field, filename, file_bytes, file_ctype):
    """multipart/form-data для загрузки файла (тот же приём, что в speech-to-text)."""
    boundary = "----rec" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        if v is None:
            continue
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, k, v)).encode("utf-8")
    out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: %s\r\n\r\n" % (boundary, file_field, filename, file_ctype)).encode("utf-8")
    out += file_bytes
    out += ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return boundary, bytes(out)


def call_scribe(file_bytes, filename, language=None):
    fields = {
        "model_id": STT_MODEL,
        "timestamps_granularity": "none",
        "diarize": "false",
        "tag_audio_events": "false",
    }
    if language and language != "auto":
        fields["language_code"] = language
    boundary, body = build_multipart(fields, "file", filename, file_bytes, "application/octet-stream")
    req = urllib.request.Request(
        ELEVEN_BASE + "/v1/speech-to-text",
        data=body,
        headers={
            "xi-api-key": API_KEY,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Accept": "application/json",
            "User-Agent": UA,
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=300)
    return json.loads(resp.read().decode("utf-8"))


def call_scribe_retry(file_bytes, filename, language=None, attempts=5):
    last = None
    for a in range(attempts):
        try:
            return call_scribe(file_bytes, filename, language=language)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503, 504) and a < attempts - 1:
                last = e
                time.sleep(1.5 * (a + 1))
                continue
            raise
        except (urllib.error.URLError, OSError) as e:
            last = e
            if a < attempts - 1:
                time.sleep(1.2 * (a + 1))
                continue
            raise
    if last:
        raise last


# ----------------------------- Telegram -----------------------------
def tg_send_message(text):
    url = "https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    resp = urllib.request.urlopen(req, timeout=60)
    return json.loads(resp.read().decode("utf-8"))


def tg_send_audio(file_bytes, filename, caption=None):
    """Отправляет само голосовое как документ (audio) в чат."""
    url = "https://api.telegram.org/bot%s/sendDocument" % BOT_TOKEN
    fields = {"chat_id": CHAT_ID}
    if caption:
        fields["caption"] = caption[:1000]
    boundary, body = build_multipart(fields, "document", filename, file_bytes, "audio/mp4")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=180)
    return json.loads(resp.read().decode("utf-8"))


def chunk_text(text, limit=3800):
    """Telegram ограничивает сообщение 4096 символами — режем длинные расшифровки."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for word in text.split(" "):
        if len(cur) + len(word) + 1 > limit:
            parts.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        parts.append(cur)
    return parts


# ----------------------------- HTTP -----------------------------
def human_dt(ts_ms):
    try:
        t = time.localtime(int(ts_ms) / 1000)
        return time.strftime("%d.%m.%Y %H:%M", t)
    except Exception:
        return ""


def human_dur(sec):
    try:
        sec = int(sec)
        return "%d:%02d" % (sec // 60, sec % 60)
    except Exception:
        return ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # тихо

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        # health-check (Render пингует корень)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/upload":
            return self._json(404, {"error": "not found"})

        q = urllib.parse.parse_qs(parsed.query)
        key = (q.get("key", [""])[0] or "").strip()
        if UPLOAD_SECRET and key != UPLOAD_SECRET:
            return self._json(403, {"error": "bad key"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._json(400, {"error": "empty body"})

        audio = self.rfile.read(length)
        if len(audio) < 500:
            return self._json(400, {"error": "audio too small"})

        ts = q.get("ts", [""])[0]
        dur = q.get("dur", [""])[0]
        ext = q.get("ext", ["m4a"])[0]
        part = q.get("part", ["1"])[0]
        filename = "rec_%s.%s" % (ts or "x", ext)

        # 1) расшифровка
        try:
            result = call_scribe_retry(audio, filename, language=(STT_LANGUAGE or None))
            text = (result.get("text") or "").strip()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            return self._json(502, {"error": "scribe %d" % e.code, "detail": detail})
        except Exception as e:
            return self._json(502, {"error": "scribe: %s" % e})

        # 2) в Telegram
        meta = [x for x in (human_dt(ts), human_dur(dur)) if x]
        # часть > 1 = запись прерывалась (экран гас) и продолжилась новым куском
        try:
            if int(part) > 1:
                meta.append("часть %d" % int(part))
        except ValueError:
            pass
        head = "🎙 Запись"
        if meta:
            head += "  (" + " · ".join(meta) + ")"

        try:
            if not text:
                tg_send_message(head + "\n\n(речь не распознана / тишина)")
            else:
                parts = chunk_text(text)
                first = True
                for p in parts:
                    tg_send_message((head + "\n\n" + p) if first else p)
                    first = False
            if SEND_AUDIO:
                try:
                    tg_send_audio(audio, filename, caption=None)
                except Exception:
                    pass  # аудио — не критично, текст уже ушёл
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            return self._json(502, {"error": "telegram %d" % e.code, "detail": detail})
        except Exception as e:
            return self._json(502, {"error": "telegram: %s" % e})

        return self._json(200, {"ok": True, "chars": len(text)})


def main():
    load_key()
    missing = []
    if not API_KEY:
        missing.append("ELEVENLABS_API_KEY")
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        print("⚠️  Не заданы: " + ", ".join(missing) + " — сервер поднимется, но /upload вернёт ошибку.")
    print("Слушаю http://%s:%d  (POST /upload)" % (HOST, PORT))
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

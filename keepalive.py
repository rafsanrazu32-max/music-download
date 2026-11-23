"""Render-এর জন্য HTTP হেলথ সার্ভার ($PORT) এবং অটো keepalive পিং।"""
import os
import time
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INTERVAL = 14 * 60


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - XZ Music Bot is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        # অপ্রয়োজনীয় HTTP অ্যাক্সেস লগ বন্ধ রাখা
        return


def detect_url():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        return url.rstrip("/")
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if host:
        return "https://" + host
    name = os.environ.get("RENDER_SERVICE_NAME")
    if name:
        return f"https://{name}.onrender.com"
    return os.environ.get("PING_URL")


def pinger_loop():
    url = detect_url()
    if not url:
        print("[keepalive] Render URL পাওয়া যায়নি, অটো-পিং বন্ধ।", flush=True)
        return

    print(f"[keepalive] URL detected: {url} | every {INTERVAL // 60} min", flush=True)
    while True:
        time.sleep(INTERVAL)
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                print(f"[keepalive] ping OK ({resp.status}) {time.strftime('%H:%M:%S')}", flush=True)
        except Exception as e:
            print(f"[keepalive] ping failed: {e}", flush=True)


def main():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[keepalive] Health server listening on 0.0.0.0:{port}", flush=True)

    t = threading.Thread(target=pinger_loop, daemon=True)
    t.start()

    server.serve_forever()


if __name__ == "__main__":
    main()

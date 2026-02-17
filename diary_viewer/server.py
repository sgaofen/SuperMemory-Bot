"""
Diary Viewer — 本地 Web 服务器

提供:
  1. REST API 读取 data/diary/ 下的日记文件
  2. 静态文件服务 (diary_viewer/)
  3. 日记生成触发 API

运行: python diary_viewer/server.py
访问: http://localhost:8888
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DIARY_DIR = DATA_DIR / "diary"
VIEWER_DIR = Path(__file__).resolve().parent

PORT = int(os.getenv("DIARY_VIEWER_PORT", "8888"))


class DiaryViewerHandler(SimpleHTTPRequestHandler):
    """Serve static files + diary API."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VIEWER_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/diaries":
            self._handle_list_diaries(parsed)
        elif path.startswith("/api/diary/"):
            date_str = path.replace("/api/diary/", "").strip("/")
            self._handle_get_diary(date_str)
        elif path == "/api/calendar":
            self._handle_calendar(parsed)
        else:
            super().do_GET()

    def _handle_list_diaries(self, parsed):
        """List all diary entries."""
        qs = parse_qs(parsed.query)
        month = qs.get("month", [None])[0]
        limit = int(qs.get("limit", ["50"])[0])

        entries = []
        if month:
            try:
                year, mon = month.split("-")
                search_dir = DIARY_DIR / year / mon
                if search_dir.is_dir():
                    md_files = sorted(search_dir.glob("diary_*.md"), reverse=True)
                else:
                    md_files = []
            except (ValueError, OSError):
                md_files = []
        else:
            md_files = sorted(DIARY_DIR.rglob("diary_*.md"), reverse=True)

        for path in md_files[:limit]:
            date_str = path.stem.replace("diary_", "")
            try:
                content = path.read_text(encoding="utf-8")
                preview = ""
                for line in content.split("\n"):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and not stripped.startswith("*"):
                        preview = stripped[:120]
                        break
                entries.append({
                    "date": date_str,
                    "size": len(content),
                    "preview": preview,
                })
            except Exception:
                entries.append({"date": date_str, "size": 0, "preview": ""})

        self._json_response(entries)

    def _handle_get_diary(self, date_str):
        """Get a single diary entry."""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            year = str(dt.year)
            month = f"{dt.month:02d}"
            path = DIARY_DIR / year / month / f"diary_{date_str}.md"
        except ValueError:
            self._json_response({"error": "Invalid date format"}, status=400)
            return

        if path.exists():
            content = path.read_text(encoding="utf-8")
            self._json_response({"date": date_str, "content": content})
        else:
            self._json_response({"error": "Diary not found"}, status=404)

    def _handle_calendar(self, parsed):
        """Get calendar data: which dates have diary entries."""
        qs = parse_qs(parsed.query)
        year = int(qs.get("year", [datetime.now().year])[0])
        month = int(qs.get("month", [datetime.now().month])[0])

        month_dir = DIARY_DIR / str(year) / f"{month:02d}"
        days = []
        if month_dir.is_dir():
            for f in month_dir.glob("diary_*.md"):
                try:
                    d = datetime.strptime(f.stem.replace("diary_", ""), "%Y-%m-%d")
                    days.append(d.day)
                except ValueError:
                    pass

        self._json_response({
            "year": year,
            "month": month,
            "days_with_diary": sorted(days),
        })

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Quieter logging."""
        if "/api/" in str(args[0]) if args else False:
            return
        super().log_message(format, *args)


def main():
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), DiaryViewerHandler)
    print(f"📔 Diary Viewer running at http://localhost:{PORT}")
    print(f"   Diary directory: {DIARY_DIR}")
    print(f"   Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Diary Viewer stopped")
        server.server_close()


if __name__ == "__main__":
    main()

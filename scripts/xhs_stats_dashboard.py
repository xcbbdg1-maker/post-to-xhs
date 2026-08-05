#!/usr/bin/env python3
"""Small Windows dashboard for cached and live Xiaohongshu creator metrics.

The dashboard is deliberately read-only. It reuses the post-to-xhs CDP client
to read the creator data table and account totals, stores timestamped CSV/JSON
snapshots, and never calls publishing or interaction commands.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
STATS_DIR = Path.home() / ".codex" / "xiaohongshu-stats"
LATEST_CSV = STATS_DIR / "latest.csv"
LATEST_ACCOUNT_JSON = STATS_DIR / "latest_account.json"
PAGE_SIZE = 10
CDP_PORT = 9222
CREATOR_STATS_PREFIX = "https://creator.xiaohongshu.com/statistics"
CREATOR_HOME_PREFIX = "https://creator.xiaohongshu.com/new/home"

CSV_FIELDS = [
    "标题",
    "发布时间",
    "曝光",
    "观看",
    "封面点击率",
    "点赞",
    "评论",
    "收藏",
    "涨粉",
    "分享",
    "人均观看时长",
    "弹幕",
    "操作",
    "_id",
]

# Cool paper + a compact set of signal colors for the read-only dashboard.
COLORS = {
    "paper": "#F3F7F8",
    "panel": "#FFFFFF",
    "ink": "#172638",
    "muted": "#71808F",
    "line": "#DCE6EA",
    "coral": "#FF5967",
    "coral_soft": "#FFE7EA",
    "mint": "#26B985",
    "mint_soft": "#DDF5EB",
    "violet": "#7168D9",
    "warning": "#D59024",
}


class LoginRequired(RuntimeError):
    """Raised when the dedicated Xiaohongshu Chrome profile is logged out."""


@dataclass
class Snapshot:
    """Loaded creator metrics and their local source file."""

    rows: list[dict[str, str]]
    source: Path
    captured_at: datetime
    account: dict[str, Any]


def optional_count(value: Any) -> int | None:
    """Parse a count, including compact Chinese units, without hiding missing data."""
    text = "" if value is None else str(value)
    text = text.replace(",", "").strip()
    if not text or text in {"-", "—"}:
        return None

    multiplier = 1
    for suffix, factor in (("万", 10_000), ("亿", 100_000_000)):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)].strip()
            break
    try:
        return round(float(text) * multiplier)
    except (TypeError, ValueError):
        return None


def count_value(value: Any) -> int:
    """Parse a creator metric such as ``1,234`` or ``-`` into an integer."""
    parsed = optional_count(value)
    return parsed if parsed is not None else 0


def format_count(value: int) -> str:
    return f"{value:,}"


def format_metric(value: Any) -> str:
    """Format one note metric while preserving the difference from missing data."""
    parsed = optional_count(value)
    return format_count(parsed) if parsed is not None else "—"


def publish_time_value(value: Any) -> datetime:
    """Parse creator-table publish times for reliable latest-note selection."""
    text = str(value or "").strip().replace("/", "-")
    if not text:
        return datetime.min
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.min


def snapshot_candidates() -> list[Path]:
    """Return usable snapshots, newest first, without counting latest twice."""
    if not STATS_DIR.exists():
        return []
    candidates = [
        path
        for path in STATS_DIR.glob("*.csv")
        if path.is_file()
        and (path.name == "latest.csv" or path.name.endswith("_all.csv"))
    ]
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)


def account_metadata_path(snapshot_path: Path) -> Path:
    if snapshot_path.name == LATEST_CSV.name:
        return LATEST_ACCOUNT_JSON
    return snapshot_path.with_name(snapshot_path.stem + "_account.json")


def load_account_metadata(snapshot_path: Path) -> dict[str, Any]:
    metadata_path = account_metadata_path(snapshot_path)
    if not metadata_path.exists():
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_snapshot(path: Path | None = None) -> Snapshot | None:
    """Load the latest cached metrics snapshot."""
    candidates = [path] if path is not None else snapshot_candidates()
    for candidate in candidates:
        if candidate is None or not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or "标题" not in reader.fieldnames:
                    continue
                rows = [dict(row) for row in reader]
            if not rows:
                continue
            captured_at = datetime.fromtimestamp(candidate.stat().st_mtime)
            return Snapshot(
                rows=rows,
                source=candidate,
                captured_at=captured_at,
                account=load_account_metadata(candidate),
            )
        except (OSError, csv.Error, UnicodeError):
            continue
    return None


def summarize(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Build the small set of metrics shown in the window."""
    totals = {
        "posts": len(rows),
        "likes": sum(count_value(row.get("点赞")) for row in rows),
        "favorites": sum(count_value(row.get("收藏")) for row in rows),
        "views": sum(count_value(row.get("观看")) for row in rows),
        "exposure": sum(count_value(row.get("曝光")) for row in rows),
        "comments": sum(count_value(row.get("评论")) for row in rows),
        "shares": sum(count_value(row.get("分享")) for row in rows),
        "followers_gained": sum(count_value(row.get("涨粉")) for row in rows),
    }
    ranked = sorted(
        rows,
        key=lambda row: (
            count_value(row.get("点赞")) + count_value(row.get("收藏")),
            count_value(row.get("观看")),
        ),
        reverse=True,
    )
    totals["top"] = ranked[:5]
    totals["likes_favorites"] = totals["likes"] + totals["favorites"]
    dated_rows = [row for row in rows if str(row.get("发布时间") or "").strip()]
    totals["latest"] = (
        max(dated_rows, key=lambda row: publish_time_value(row.get("发布时间")))
        if dated_rows
        else (rows[0] if rows else {})
    )
    totals["like_rate"] = (
        totals["likes"] * 100.0 / totals["views"] if totals["views"] else 0.0
    )
    totals["favorite_rate"] = (
        totals["favorites"] * 100.0 / totals["views"]
        if totals["views"]
        else 0.0
    )
    return totals


def write_csv_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a UTF-8 CSV and atomically replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON metadata without exposing partial files to the dashboard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def save_snapshot(
    rows: list[dict[str, Any]],
    account: dict[str, Any] | None = None,
) -> Path:
    """Save a timestamped snapshot plus the instant-loading latest cache."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    snapshot_path = STATS_DIR / f"content_data_{stamp}_all.csv"
    account_payload = {
        "schema_version": 1,
        "snapshot_captured_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        **(account or {}),
    }
    write_csv_atomic(snapshot_path, rows)
    write_json_atomic(account_metadata_path(snapshot_path), account_payload)
    write_csv_atomic(LATEST_CSV, rows)
    write_json_atomic(LATEST_ACCOUNT_JSON, account_payload)
    return snapshot_path


def fetch_creator_account_metrics(publisher: Any) -> dict[str, Any]:
    """Read current account totals from the logged-in creator-center session."""
    response = publisher._evaluate("""
        (async () => {
            const result = await fetch(
                "/api/galaxy/creator/home/personal_info",
                {credentials: "include", cache: "no-store"}
            );
            let body = null;
            try {
                body = await result.json();
            } catch (error) {
                body = null;
            }
            return {ok: result.ok, status: result.status, body};
        })()
    """)
    if not isinstance(response, dict) or not response.get("ok"):
        status = response.get("status") if isinstance(response, dict) else "unknown"
        raise RuntimeError(f"账户概览接口读取失败：HTTP {status}")

    body = response.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("账户概览接口没有返回有效数据")

    followers = optional_count(data.get("fans_count"))
    if followers is None:
        grow_info = data.get("grow_info")
        if isinstance(grow_info, dict):
            followers = optional_count(grow_info.get("fans_count"))
    if followers is None:
        raise RuntimeError("账户概览未包含粉丝总数")

    return {
        "followers": followers,
        "platform_likes_favorites": optional_count(data.get("faved_count")),
        "nickname": str(data.get("name") or ""),
        "red_num": str(data.get("red_num") or ""),
        "account_captured_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "followers_stale": False,
    }


def _page_fingerprint(rows: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(row.get("标题") or ""), str(row.get("发布时间") or ""))
        for row in rows
    )


def _fetch_page_with_retry(
    publisher: Any,
    page_num: int,
    progress: Callable[[str], None],
    attempts: int = 3,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            payload = publisher.get_content_data(
                page_num=page_num,
                page_size=PAGE_SIZE,
                note_type=0,
            )
            resolved_page = int(payload.get("resolved_page_num") or page_num)
            if resolved_page != page_num:
                raise RuntimeError(
                    f"分页校验失败：请求第 {page_num} 页，实际返回第 {resolved_page} 页"
                )
            rows = payload.get("rows")
            if not isinstance(rows, list) or not rows:
                raise RuntimeError(f"第 {page_num} 页没有返回笔记数据")
            return payload
        except Exception as exc:  # CDP and page variants need the same retry path.
            last_error = exc
            if attempt < attempts:
                progress(f"第 {page_num} 页暂未响应，正在重试 {attempt}/{attempts - 1}")
                time.sleep(0.8)
    raise RuntimeError(f"第 {page_num} 页读取失败：{last_error}") from last_error


def connect_dashboard_session(
    publisher: Any,
    progress: Callable[[str], None],
    attempts: int = 2,
) -> bool:
    """Connect to a non-publish creator tab and tolerate one closed-target retry."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            targets = publisher._get_targets()
            page_urls = [
                str(target.get("url") or "")
                for target in targets
                if target.get("type") == "page"
            ]
            safe_prefix = next(
                (
                    prefix
                    for prefix in (CREATOR_STATS_PREFIX, CREATOR_HOME_PREFIX)
                    if any(url.startswith(prefix) for url in page_urls)
                ),
                "",
            )
            publisher.connect(
                target_url_prefix=safe_prefix,
                reuse_existing_tab=False,
            )
            return bool(publisher.check_login())
        except Exception as exc:
            last_error = exc
            try:
                publisher.disconnect()
            except Exception:
                pass
            if attempt < attempts:
                progress("浏览器数据标签连接已重置，正在重试…")
                time.sleep(0.8)
    raise RuntimeError(f"无法连接创作者数据标签：{last_error}") from last_error


def collect_live_metrics(progress: Callable[[str], None]) -> Path:
    """Read every creator-data page and persist a verified local snapshot."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))

    from cdp_publish import XiaohongshuPublisher
    from chrome_launcher import is_port_open, kill_chrome, launch_chrome

    started_browser = False
    if not is_port_open(CDP_PORT):
        progress("正在启动专用浏览器…")
        launch_chrome(port=CDP_PORT, headless=True)
        started_browser = True

    publisher: Any | None = None
    try:
        progress("正在确认登录状态…")
        publisher = XiaohongshuPublisher(port=CDP_PORT)
        publisher.login_cache_ttl_seconds = 0
        if not connect_dashboard_session(publisher, progress):
            raise LoginRequired("登录已失效，请点“打开登录页”扫码后再刷新。")

        previous_snapshot = load_snapshot()
        progress("正在读取账户粉丝数…")
        try:
            account_metrics = fetch_creator_account_metrics(publisher)
        except Exception:
            previous_account = previous_snapshot.account if previous_snapshot else {}
            if optional_count(previous_account.get("followers")) is not None:
                account_metrics = dict(previous_account)
                account_metrics["followers_stale"] = True
                progress("粉丝数暂未更新，沿用上次缓存…")
            else:
                account_metrics = {"followers_stale": True}
                progress("粉丝数暂不可用，继续刷新笔记数据…")

        progress("正在读取第 1 页…")
        first_payload = _fetch_page_with_retry(publisher, 1, progress)
        first_rows = list(first_payload.get("rows") or [])
        total = int(first_payload.get("total") or len(first_rows))
        page_count = max(1, math.ceil(total / PAGE_SIZE))

        all_rows = first_rows
        fingerprints = {_page_fingerprint(first_rows)}
        for page_num in range(2, page_count + 1):
            progress(f"正在读取第 {page_num}/{page_count} 页…")
            payload = _fetch_page_with_retry(publisher, page_num, progress)
            page_rows = list(payload.get("rows") or [])
            fingerprint = _page_fingerprint(page_rows)
            if fingerprint in fingerprints:
                raise RuntimeError(f"第 {page_num} 页与前页重复，已停止以避免误统计。")
            fingerprints.add(fingerprint)
            all_rows.extend(page_rows)

        if len(all_rows) != total:
            raise RuntimeError(
                f"完整性校验失败：平台显示 {total} 篇，实际读取 {len(all_rows)} 篇。"
            )

        summary = summarize(all_rows)
        platform_total = optional_count(
            account_metrics.get("platform_likes_favorites")
        )
        if platform_total is not None:
            account_metrics["likes_favorites_match"] = (
                platform_total == summary["likes_favorites"]
            )

        progress("正在保存完整快照…")
        return save_snapshot(all_rows, account=account_metrics)
    finally:
        if publisher is not None:
            try:
                publisher.disconnect()
            except Exception:
                pass
        if started_browser:
            kill_chrome(port=CDP_PORT)


def open_login_page(progress: Callable[[str], None]) -> None:
    """Open the dedicated headed Chrome profile on the creator login page."""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))

    from cdp_publish import XiaohongshuPublisher
    from chrome_launcher import restart_chrome

    progress("正在打开登录页…")
    restart_chrome(port=CDP_PORT, headless=False)
    publisher = XiaohongshuPublisher(port=CDP_PORT)
    try:
        publisher.connect(reuse_existing_tab=True)
        publisher.open_login_page()
    finally:
        publisher.disconnect()


def enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class StatsDashboard(tk.Tk):
    """A compact creator metrics window backed by local CSV snapshots."""

    def __init__(self) -> None:
        super().__init__()
        self.title("小红书数据看板")
        self.display_scale = max(
            1.0,
            float(self.tk.call("tk", "scaling")) / (96.0 / 72.0),
        )
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        self.default_width = min(
            round(650 * self.display_scale), max(650, screen_width - 40)
        )
        self.default_height = min(
            round(460 * self.display_scale), max(460, screen_height - 80)
        )
        self.geometry(f"{self.default_width}x{self.default_height}")
        self.configure(bg=COLORS["paper"])
        self._set_window_icon()

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.current_snapshot: Snapshot | None = None
        self.refreshing = False
        self.status_phase = 0

        self.status_var = tk.StringVar(value="正在读取本地快照…")
        self.updated_var = tk.StringVar(value="尚无快照")
        self.likes_var = tk.StringVar(value="—")
        self.favorites_var = tk.StringVar(value="—")
        self.likes_favorites_var = tk.StringVar(value="—")
        self.followers_var = tk.StringVar(value="—")
        self.latest_title_var = tk.StringVar(value="暂无笔记")
        self.latest_meta_var = tk.StringVar(value="刷新后显示最新发布数据")
        self.latest_likes_var = tk.StringVar(value="—")
        self.latest_favorites_var = tk.StringVar(value="—")
        self.latest_comments_var = tk.StringVar(value="—")
        self.latest_shares_var = tk.StringVar(value="—")

        self._configure_styles()
        self._build_layout()
        self.update_idletasks()
        natural_width = self.winfo_reqwidth()
        natural_height = self.winfo_reqheight()
        self.default_width = min(
            max(self.default_width, natural_width), max(650, screen_width - 40)
        )
        self.default_height = min(
            max(round(340 * self.display_scale), natural_height),
            max(460, screen_height - 80),
        )
        self.minsize(
            min(
                max(round(600 * self.display_scale), natural_width),
                self.default_width,
            ),
            self.default_height,
        )
        self.geometry(f"{self.default_width}x{self.default_height}")
        self.bind("<F5>", lambda _event: self.refresh_data())
        self.bind("<Control-r>", lambda _event: self.refresh_data())
        self.bind("<Control-R>", lambda _event: self.refresh_data())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_cached_snapshot()
        self.after_idle(self._center_window)
        self.after(260, self._present_window)
        self.after(120, self._poll_events)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        table_font = tkfont.Font(
            root=self,
            family="Microsoft YaHei UI",
            size=9,
        )
        self.table_row_height = max(26, table_font.metrics("linespace") + 6)
        self.table_column_widths = {
            "likes": max(54, table_font.measure("9,999") + 14),
            "favorites": max(54, table_font.measure("9,999") + 14),
            "shares": max(54, table_font.measure("9,999") + 14),
            "views": max(62, table_font.measure("99,999") + 14),
        }
        style.configure(
            "Stats.Treeview",
            background=COLORS["panel"],
            fieldbackground=COLORS["panel"],
            foreground=COLORS["ink"],
            rowheight=self.table_row_height,
            borderwidth=0,
            relief="flat",
            font=table_font,
        )
        style.configure(
            "Stats.Treeview.Heading",
            background=COLORS["paper"],
            foreground=COLORS["muted"],
            borderwidth=0,
            relief="flat",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "Stats.Treeview",
            background=[("selected", COLORS["mint_soft"])],
            foreground=[("selected", COLORS["ink"])],
        )

    def _set_window_icon(self) -> None:
        """Draw a tiny red notebook with a mint bookmark for the title bar."""
        icon = tk.PhotoImage(width=32, height=32)
        icon.put(COLORS["coral"], to=(4, 3, 27, 29))
        icon.put("#D94352", to=(4, 3, 8, 29))
        icon.put("#FFFFFF", to=(9, 7, 24, 26))
        icon.put(COLORS["mint"], to=(18, 3, 23, 15))
        icon.put(COLORS["line"], to=(11, 11, 21, 12))
        icon.put(COLORS["line"], to=(11, 16, 21, 17))
        self._window_icon = icon
        self.iconphoto(True, icon)

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = tk.Frame(self, bg=COLORS["paper"])
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(9, 7))
        header.grid_columnconfigure(0, weight=1)

        title_block = tk.Frame(header, bg=COLORS["paper"])
        title_block.grid(row=0, column=0, sticky="w")
        tk.Label(
            title_block,
            text="创作数据",
            bg=COLORS["paper"],
            fg=COLORS["ink"],
            font=("Bahnschrift SemiBold", 20),
        ).pack(side="left", anchor="s")
        tk.Label(
            title_block,
            text="小红书笔记表现 · 本地只读看板",
            bg=COLORS["paper"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", anchor="s", padx=(12, 0), pady=(0, 3))

        actions = tk.Frame(header, bg=COLORS["paper"])
        actions.grid(row=0, column=1, sticky="e")
        self.refresh_button = tk.Button(
            actions,
            text="刷新数据",
            command=self.refresh_data,
            bg=COLORS["ink"],
            fg="#FFFFFF",
            activebackground="#263A50",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=12,
            pady=6,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.refresh_button.grid(row=0, column=0)

        latest_panel = tk.Frame(self, bg=COLORS["panel"], highlightthickness=1)
        latest_panel.configure(highlightbackground=COLORS["line"])
        latest_panel.grid(row=1, column=0, sticky="ew", padx=16)
        latest_panel.grid_columnconfigure(
            0, weight=0, minsize=round(290 * self.display_scale)
        )
        latest_panel.grid_columnconfigure(1, weight=0)

        latest_note = tk.Frame(latest_panel, bg=COLORS["panel"])
        latest_note.grid(row=0, column=0, sticky="nsew", padx=(14, 10), pady=10)
        tk.Label(
            latest_note,
            text="最新发布",
            bg=COLORS["panel"],
            fg=COLORS["coral"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")
        self.latest_title_label = tk.Label(
            latest_note,
            textvariable=self.latest_title_var,
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            justify="left",
            anchor="w",
            wraplength=round(290 * self.display_scale),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.latest_title_label.pack(anchor="w", fill="x", pady=(3, 0))
        tk.Label(
            latest_note,
            textvariable=self.latest_meta_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify="left",
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", pady=(3, 0))

        latest_metrics = tk.Frame(latest_panel, bg=COLORS["panel"])
        latest_metrics.grid(row=0, column=1, sticky="w", padx=(0, 6))
        for column in range(4):
            latest_metrics.grid_columnconfigure(
                column, weight=0, minsize=round(58 * self.display_scale)
            )
        self._latest_metric_cell(
            latest_metrics, 0, "点赞", self.latest_likes_var, COLORS["coral"]
        )
        self._latest_metric_cell(
            latest_metrics, 1, "收藏", self.latest_favorites_var, COLORS["mint"]
        )
        self._latest_metric_cell(
            latest_metrics, 2, "评论", self.latest_comments_var, COLORS["violet"]
        )
        self._latest_metric_cell(
            latest_metrics, 3, "转发", self.latest_shares_var, COLORS["warning"]
        )

        metric_band = tk.Frame(self, bg=COLORS["panel"], highlightthickness=1)
        metric_band.configure(highlightbackground=COLORS["line"])
        metric_band.grid(row=2, column=0, sticky="ew", padx=16, pady=8)
        for column in range(4):
            metric_band.grid_columnconfigure(column, weight=1)

        self._metric_cell(
            metric_band,
            column=0,
            label="总点赞",
            variable=self.likes_var,
            color=COLORS["coral"],
        )
        self._metric_cell(
            metric_band,
            column=1,
            label="总收藏",
            variable=self.favorites_var,
            color=COLORS["mint"],
        )
        self._metric_cell(
            metric_band,
            column=2,
            label="总点赞和收藏",
            variable=self.likes_favorites_var,
            color=COLORS["coral"],
            secondary_color=COLORS["mint"],
        )
        self._metric_cell(
            metric_band,
            column=3,
            label="粉丝数",
            variable=self.followers_var,
            color=COLORS["violet"],
        )

        table_panel = tk.Frame(self, bg=COLORS["panel"], highlightthickness=1)
        table_panel.configure(highlightbackground=COLORS["line"])
        table_panel.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 8))
        table_panel.grid_columnconfigure(0, weight=1)
        table_panel.grid_rowconfigure(1, weight=1)

        table_header = tk.Frame(table_panel, bg=COLORS["panel"])
        table_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        tk.Label(
            table_header,
            text="表现靠前的笔记",
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")
        tk.Label(
            table_header,
            text="按点赞 + 收藏排序",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right")

        columns = ("title", "likes", "favorites", "shares", "views")
        self.table = ttk.Treeview(
            table_panel,
            columns=columns,
            show="headings",
            style="Stats.Treeview",
            selectmode="browse",
            height=5,
        )
        self.table.heading("title", text="笔记", anchor="w")
        self.table.heading("likes", text="点赞", anchor="e")
        self.table.heading("favorites", text="收藏", anchor="e")
        self.table.heading("shares", text="转发", anchor="e")
        self.table.heading("views", text="观看", anchor="e")
        self.table.column("title", width=360, minwidth=180, anchor="w", stretch=True)
        for column in ("likes", "favorites", "shares", "views"):
            width = self.table_column_widths[column]
            self.table.column(
                column,
                width=width,
                minwidth=width,
                anchor="e",
                stretch=False,
            )
        self.table.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.table.bind("<Configure>", self._resize_table_columns)

        footer = tk.Frame(self, bg=COLORS["paper"])
        footer.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 10))
        footer.grid_columnconfigure(0, weight=1)
        footer_status = tk.Frame(footer, bg=COLORS["paper"])
        footer_status.grid(row=0, column=0, sticky="w")
        self.status_dot = tk.Canvas(
            footer_status,
            width=12,
            height=12,
            bg=COLORS["paper"],
            highlightthickness=0,
        )
        self.status_dot.pack(side="left", padx=(0, 5))
        self.status_circle = self.status_dot.create_oval(
            2, 2, 10, 10, fill=COLORS["mint"], outline=""
        )
        tk.Label(
            footer_status,
            textvariable=self.status_var,
            bg=COLORS["paper"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")
        tk.Label(
            footer_status,
            text="  ·  ",
            bg=COLORS["paper"],
            fg=COLORS["line"],
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")
        tk.Label(
            footer_status,
            textvariable=self.updated_var,
            bg=COLORS["paper"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")
        self.login_button = tk.Button(
            footer,
            text="打开登录页",
            command=self.open_login,
            bg=COLORS["paper"],
            fg=COLORS["violet"],
            activebackground=COLORS["paper"],
            activeforeground=COLORS["ink"],
            relief="flat",
            bd=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.login_button.grid(row=0, column=1, padx=(8, 12))
        self.open_button = tk.Button(
            footer,
            text="打开明细 CSV",
            command=self.open_csv,
            bg=COLORS["paper"],
            fg=COLORS["ink"],
            activebackground=COLORS["paper"],
            activeforeground=COLORS["violet"],
            relief="flat",
            bd=0,
            cursor="hand2",
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.open_button.grid(row=0, column=2)

    def _metric_cell(
        self,
        parent: tk.Widget,
        column: int,
        label: str,
        variable: tk.StringVar,
        color: str,
        secondary_color: str | None = None,
    ) -> None:
        frame = tk.Frame(parent, bg=COLORS["panel"])
        frame.grid(row=0, column=column, sticky="nsew", pady=10)
        if column:
            tk.Frame(frame, bg=COLORS["line"], width=1).pack(side="left", fill="y")
        content = tk.Frame(frame, bg=COLORS["panel"])
        content.pack(side="left", fill="both", expand=True, padx=(14, 10))
        accent = tk.Frame(content, bg=COLORS["panel"], height=2, width=28)
        accent.pack(anchor="w", pady=(0, 5))
        accent.pack_propagate(False)
        if secondary_color:
            tk.Frame(accent, bg=color).pack(side="left", fill="both", expand=True)
            tk.Frame(accent, bg=secondary_color).pack(
                side="left", fill="both", expand=True
            )
        else:
            tk.Frame(accent, bg=color).pack(fill="both", expand=True)
        tk.Label(
            content,
            text=label,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w")
        tk.Label(
            content,
            textvariable=variable,
            bg=COLORS["panel"],
            fg=COLORS["ink"],
            font=("Bahnschrift SemiBold", 24),
        ).pack(anchor="w", pady=(1, 0))

    def _latest_metric_cell(
        self,
        parent: tk.Widget,
        column: int,
        label: str,
        variable: tk.StringVar,
        color: str,
    ) -> None:
        frame = tk.Frame(parent, bg=COLORS["panel"])
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 2), pady=10)
        tk.Frame(frame, bg=COLORS["line"], width=1).pack(side="left", fill="y")
        content = tk.Frame(frame, bg=COLORS["panel"])
        content.pack(side="left", fill="both", expand=True, padx=(8, 6))
        tk.Label(
            content,
            text=label,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w")
        tk.Label(
            content,
            textvariable=variable,
            bg=COLORS["panel"],
            fg=color,
            font=("Bahnschrift SemiBold", 18),
        ).pack(anchor="w", pady=(4, 0))

    def _resize_table_columns(self, event: tk.Event) -> None:
        fixed_width = sum(self.table_column_widths.values())
        title_width = max(180, int(event.width) - fixed_width - 8)
        self.table.column("title", width=title_width)

    def _load_cached_snapshot(self) -> None:
        snapshot = load_snapshot()
        if snapshot is None:
            self.status_var.set("没有本地快照")
            self.updated_var.set("点“刷新数据”创建第一份只读统计")
            self.status_dot.itemconfigure(self.status_circle, fill=COLORS["warning"])
            return
        self._render_snapshot(snapshot)
        self.status_var.set("本地快照已载入")
        self.status_dot.itemconfigure(self.status_circle, fill=COLORS["mint"])

    def _render_snapshot(self, snapshot: Snapshot) -> None:
        self.current_snapshot = snapshot
        summary = summarize(snapshot.rows)
        self.likes_var.set(format_count(summary["likes"]))
        self.favorites_var.set(format_count(summary["favorites"]))
        self.likes_favorites_var.set(format_count(summary["likes_favorites"]))
        followers = optional_count(snapshot.account.get("followers"))
        self.followers_var.set(
            format_count(followers) if followers is not None else "—"
        )
        latest = summary["latest"]
        if latest:
            self.latest_title_var.set(str(latest.get("标题") or "无标题笔记"))
            self.latest_meta_var.set(
                f"{latest.get('发布时间') or '发布时间未知'} · "
                f"{format_metric(latest.get('观看'))} 观看 · "
                f"{format_metric(latest.get('曝光'))} 曝光"
            )
            self.latest_likes_var.set(format_metric(latest.get("点赞")))
            self.latest_favorites_var.set(format_metric(latest.get("收藏")))
            self.latest_comments_var.set(format_metric(latest.get("评论")))
            self.latest_shares_var.set(format_metric(latest.get("分享")))
        follower_note = " · 粉丝数为上次缓存" if snapshot.account.get(
            "followers_stale"
        ) else ""
        self.updated_var.set(
            "快照时间 "
            + snapshot.captured_at.strftime("%Y-%m-%d %H:%M")
            + follower_note
        )

        for item in self.table.get_children():
            self.table.delete(item)
        for row in summary["top"]:
            title = str(row.get("标题") or "无标题笔记")
            self.table.insert(
                "",
                "end",
                values=(
                    title,
                    format_count(count_value(row.get("点赞"))),
                    format_count(count_value(row.get("收藏"))),
                    format_count(count_value(row.get("分享"))),
                    format_count(count_value(row.get("观看"))),
                ),
            )

    def _set_busy(self, busy: bool) -> None:
        self.refreshing = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.refresh_button.configure(state=state)
        self.refresh_button.configure(text="刷新中…" if busy else "刷新数据")
        self.login_button.configure(state=state)
        if busy:
            self._animate_status()
        else:
            self.status_dot.itemconfigure(self.status_circle, fill=COLORS["mint"])

    def _animate_status(self) -> None:
        if not self.refreshing:
            return
        colors = [COLORS["coral"], COLORS["violet"], COLORS["mint"]]
        self.status_dot.itemconfigure(
            self.status_circle, fill=colors[self.status_phase % len(colors)]
        )
        self.status_phase += 1
        self.after(420, self._animate_status)

    def refresh_data(self) -> None:
        if self.refreshing:
            return
        self._set_busy(True)
        self.status_var.set("准备刷新…")

        def worker() -> None:
            try:
                path = collect_live_metrics(
                    lambda text: self.events.put(("progress", text))
                )
                self.events.put(("done", path))
            except LoginRequired as exc:
                self.events.put(("login_required", str(exc)))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def open_login(self) -> None:
        if self.refreshing:
            return
        self._set_busy(True)

        def worker() -> None:
            try:
                open_login_page(lambda text: self.events.put(("progress", text)))
                self.events.put(("login_open", None))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def open_csv(self) -> None:
        if self.current_snapshot is None:
            messagebox.showinfo("暂无明细", "先点“刷新数据”生成一份统计快照。")
            return
        try:
            os.startfile(self.current_snapshot.source)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开明细", str(exc))

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    self.status_var.set(str(payload))
                elif event == "done":
                    snapshot = load_snapshot(Path(payload))
                    if snapshot is None:
                        raise RuntimeError("刷新完成，但快照文件无法读取。")
                    self._render_snapshot(snapshot)
                    self.status_var.set("刷新完成")
                    self._set_busy(False)
                elif event == "login_open":
                    self.status_var.set("登录页已打开")
                    self._set_busy(False)
                    messagebox.showinfo(
                        "登录页已打开",
                        "请在专用 Chrome 中扫码。登录完成后回到看板点“刷新数据”。",
                    )
                elif event == "login_required":
                    self.status_var.set("需要重新登录")
                    self.status_dot.itemconfigure(
                        self.status_circle, fill=COLORS["warning"]
                    )
                    self._set_busy(False)
                    messagebox.showwarning("需要登录", str(payload))
                elif event == "error":
                    self.status_var.set("刷新失败，缓存仍可查看")
                    self.status_dot.itemconfigure(
                        self.status_circle, fill=COLORS["warning"]
                    )
                    self._set_busy(False)
                    messagebox.showerror("读取失败", str(payload))
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _center_window(self) -> None:
        self.update_idletasks()
        width = min(
            max(self.winfo_reqwidth(), self.default_width),
            max(650, self.winfo_screenwidth() - 40),
        )
        height = min(
            max(self.winfo_reqheight(), self.default_height),
            max(460, self.winfo_screenheight() - 80),
        )
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _present_window(self) -> None:
        """Undo a minimized launch inherited from a shell or shortcut host."""
        self.deiconify()
        self.state("normal")
        self._center_window()
        self.lift()
        self.focus_force()

    def _on_close(self) -> None:
        if self.refreshing:
            messagebox.showinfo(
                "数据仍在刷新",
                "等刷新结束后再关闭窗口，旧快照会一直保留。",
            )
            return
        self.destroy()


def self_test() -> int:
    """Validate the cached-data path without opening a GUI or the browser."""
    snapshot = load_snapshot()
    if snapshot is None:
        print(json.dumps({"ok": False, "error": "no cached snapshot"}, ensure_ascii=False))
        return 1
    summary = summarize(snapshot.rows)
    result = {
        "ok": True,
        "source": str(snapshot.source),
        "rows": len(snapshot.rows),
        "likes": summary["likes"],
        "favorites": summary["favorites"],
        "likes_favorites": summary["likes_favorites"],
        "followers": optional_count(snapshot.account.get("followers")),
        "platform_likes_favorites": optional_count(
            snapshot.account.get("platform_likes_favorites")
        ),
        "likes_favorites_match": snapshot.account.get("likes_favorites_match"),
        "views": summary["views"],
        "top_title": summary["top"][0].get("标题") if summary["top"] else "",
        "top_shares": (
            count_value(summary["top"][0].get("分享")) if summary["top"] else 0
        ),
        "latest_title": summary["latest"].get("标题") if summary["latest"] else "",
        "latest_likes": count_value(summary["latest"].get("点赞")),
        "latest_favorites": count_value(summary["latest"].get("收藏")),
        "latest_comments": count_value(summary["latest"].get("评论")),
        "latest_shares": count_value(summary["latest"].get("分享")),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Xiaohongshu creator metrics dashboard")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="validate cached metrics without opening the GUI",
    )
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help="refresh all metrics, print the snapshot path, and exit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        return self_test()
    if args.refresh_only:
        path = collect_live_metrics(lambda text: print(text, flush=True))
        snapshot = load_snapshot(path)
        summary = summarize(snapshot.rows if snapshot else [])
        print(
            json.dumps(
                {
                    "snapshot": str(path),
                    "rows": summary.get("posts", 0),
                    "likes": summary.get("likes", 0),
                    "favorites": summary.get("favorites", 0),
                    "likes_favorites": summary.get("likes_favorites", 0),
                    "followers": optional_count(
                        snapshot.account.get("followers") if snapshot else None
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    enable_windows_dpi_awareness()
    from run_lock import SingleInstanceError, single_instance

    try:
        with single_instance("xhs_stats_dashboard"):
            app = StatsDashboard()
            app.mainloop()
    except SingleInstanceError:
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0,
                    "小红书数据看板已经打开。",
                    "小红书数据看板",
                    0x40,
                )
            except Exception:
                pass
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

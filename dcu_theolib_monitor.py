#!/usr/bin/env python3
"""Monitor a library new-arrivals page and notify when new items appear.

Designed for Daegu Catholic University Theology Library monitoring, but the
script is intentionally generic so it can watch any public “new arrivals”
search-results page.

Core idea:
1. Open the public TARGET_URL with Playwright (works even when the site needs
   JavaScript or blocks simple HTTP clients).
2. Extract detail-page links and nearby text from the page.
3. Compare against previously seen items saved in STATE_FILE.
4. Notify via ntfy, SMTP email, and/or Telegram when new items are detected.

Recommended usage for this specific case:
- In a normal browser, open the public DCU library new-arrivals page.
- Filter to “신학도서관” once.
- Copy the final results-page URL from the address bar.
- Put that URL into TARGET_URL.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import sys
import textwrap
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable, List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = ROOT / "state" / "seen_items.json"
DEFAULT_DEBUG_DIR = ROOT / "debug"


@dataclass
class Item:
    item_id: str
    title: str
    link: str
    block_text: str


class MonitorError(RuntimeError):
    """Raised when scraping cannot produce a usable item list."""


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


TARGET_URL = "https://lib.cu.ac.kr/newarrival?st=SUBJ&bk_2=jttjb00culibjttj&sNo=0&newDate=60&sq=&oi=&os=DESC&cpp=100"
INCLUDE_TEXT = env("INCLUDE_TEXT")
DETAIL_LINK_SELECTOR = env("DETAIL_LINK_SELECTOR", "a[href*='/search/detail/']")
STATE_FILE = Path(env("STATE_FILE", str(DEFAULT_STATE_FILE))).resolve()
DEBUG_DIR = Path(env("DEBUG_DIR", str(DEFAULT_DEBUG_DIR))).resolve()
TITLE_PREFIX = env("TITLE_PREFIX", "[DCU 신학도서관]")
PAGE_WAIT_MS = int(env("PAGE_WAIT_MS", "5000"))
MAX_ITEMS = int(env("MAX_ITEMS", "40"))
HEADLESS = env("HEADLESS", "true").lower() != "false"
USER_AGENT = env(
    "USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
)

# Notification settings
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = env("NTFY_TOPIC")

SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = int(env("SMTP_PORT", "465"))
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")
SMTP_FROM = env("SMTP_FROM", SMTP_USER)
EMAIL_TO = env("EMAIL_TO")

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

DRY_RUN = env("DRY_RUN", "false").lower() == "true"
DEBUG_ALWAYS = env("DEBUG_ALWAYS", "false").lower() == "true"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    print(message, flush=True)


def normalize_space(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def block_to_title(anchor_text: str, block_text: str) -> str:
    candidates: list[str] = []

    anchor_text = normalize_space(anchor_text)
    if anchor_text:
        candidates.append(anchor_text)

    for line in block_text.splitlines():
        line = normalize_space(line)
        if not line:
            continue
        # Skip obvious UI noise.
        if line in {"상세보기", "미리보기", "예약", "대출가능", "선택"}:
            continue
        if len(line) < 2:
            continue
        candidates.append(line)

    if not candidates:
        return "(제목 추출 실패)"

    # Prefer the shortest meaningful candidate; anchor text is usually best.
    candidates = sorted(dict.fromkeys(candidates), key=lambda s: (len(s), s))
    return candidates[0][:200]


def make_item_id(link: str, title: str) -> str:
    base = link or title
    return normalize_space(base)


def save_debug_text(name: str, text: str) -> None:
    path = DEBUG_DIR / name
    path.write_text(text, encoding="utf-8")
    log(f"[debug] wrote {path}")


def save_debug_json(name: str, data: Any) -> None:
    path = DEBUG_DIR / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[debug] wrote {path}")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "initialized_at": None,
            "last_checked_at": None,
            "target_url": TARGET_URL,
            "seen_items": [],
        }
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = STATE_FILE.with_suffix(".corrupt.json")
        STATE_FILE.replace(backup)
        log(f"[warn] State file was corrupt. Moved to {backup}")
        return {
            "initialized_at": None,
            "last_checked_at": None,
            "target_url": TARGET_URL,
            "seen_items": [],
        }


def save_state(items: list[Item], state: dict[str, Any]) -> None:
    state["initialized_at"] = state.get("initialized_at") or utc_now_iso()
    state["last_checked_at"] = utc_now_iso()
    state["target_url"] = TARGET_URL
    state["seen_items"] = [asdict(item) for item in items]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[state] saved {len(items)} seen items -> {STATE_FILE}")


JS_EXTRACT = r"""
(links) => {
  function clean(text) {
    return (text || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  }

  return links.map((el) => {
    const href = el.href || el.getAttribute('href') || '';
    const anchorText = clean(el.innerText || el.textContent || '');
    const container =
      el.closest('tr, li, article, .item, .book, .result, .results, .list-group-item, .card, .searchList, .row') ||
      el.parentElement ||
      el;
    const blockText = clean(container ? (container.innerText || container.textContent || '') : anchorText);
    return { href, anchorText, blockText };
  });
}
"""


def browser_extract(url: str) -> list[Item]:
    """Open the page with Playwright and extract candidate items."""
    ensure_dirs()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(user_agent=USER_AGENT, locale="ko-KR")
        page = context.new_page()
        try:
            log(f"[browser] opening {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(PAGE_WAIT_MS)

            # A small scroll often triggers lazy-loaded result lists.
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(1000)

            html = page.content()
            if DEBUG_ALWAYS:
                save_debug_text("last_page.html", html)
                page.screenshot(path=str(DEBUG_DIR / "last_page.png"), full_page=True)
                log(f"[debug] wrote {DEBUG_DIR / 'last_page.png'}")

            link_locator = page.locator(DETAIL_LINK_SELECTOR)
            count = link_locator.count()
            log(f"[browser] found {count} links for selector: {DETAIL_LINK_SELECTOR}")

            raw: list[dict[str, str]] = []
            if count:
                raw = link_locator.evaluate_all(JS_EXTRACT)
            else:
                # Fallback: use BeautifulSoup on the rendered HTML.
                raw = html_extract(html, base_url=url)

            items = raw_to_items(raw, base_url=url)
            if not items and not DEBUG_ALWAYS:
                save_debug_text("last_page.html", html)
                page.screenshot(path=str(DEBUG_DIR / "last_page.png"), full_page=True)
                log(f"[debug] wrote {DEBUG_DIR / 'last_page.png'}")
            if not items:
                raise MonitorError(
                    "No candidate items were extracted. "
                    "Check the selector, the TARGET_URL, or open the page manually once to verify it is public."
                )
            return items[:MAX_ITEMS]
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise MonitorError(f"Browser-based extraction failed: {exc}") from exc
        finally:
            context.close()
            browser.close()


def html_extract(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    raw: list[dict[str, str]] = []
    for a in soup.select("a[href*='/search/detail/']"):
        href = a.get("href") or ""
        href = urljoin(base_url, href)
        container = a.find_parent(["tr", "li", "article", "div"]) or a
        block_text = normalize_space(container.get_text(" ", strip=True))
        anchor_text = normalize_space(a.get_text(" ", strip=True))
        raw.append({"href": href, "anchorText": anchor_text, "blockText": block_text})
    return raw


def raw_to_items(raw: Iterable[dict[str, str]], base_url: str) -> list[Item]:
    seen: set[str] = set()
    items: list[Item] = []

    for entry in raw:
        href = normalize_space(entry.get("href", ""))
        if href and not href.startswith("http"):
            href = urljoin(base_url, href)
        anchor_text = normalize_space(entry.get("anchorText", ""))
        block_text = normalize_space(entry.get("blockText", ""))
        title = block_to_title(anchor_text, block_text)

        if INCLUDE_TEXT:
            haystack = f"{title} {block_text} {href}"
            if INCLUDE_TEXT not in haystack:
                continue

        item_id = make_item_id(href, title)
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        items.append(Item(item_id=item_id, title=title, link=href, block_text=block_text[:500]))

    if DEBUG_ALWAYS or not items:
        save_debug_json("last_items.json", [asdict(item) for item in items])
    return items


def diff_items(previous: list[Item], current: list[Item]) -> list[Item]:
    prev_ids = {item.item_id for item in previous}
    return [item for item in current if item.item_id not in prev_ids]


def format_notification(new_items: list[Item], target_url: str) -> tuple[str, str]:
    subject = f"{TITLE_PREFIX} 신착자료 {len(new_items)}건"
    lines = [
        f"새로 감지된 신착자료: {len(new_items)}건",
        "",
    ]
    for idx, item in enumerate(new_items[:10], start=1):
        lines.append(f"{idx}. {item.title}")
        if item.link:
            lines.append(f"   {item.link}")
    if len(new_items) > 10:
        lines.append("")
        lines.append(f"…외 {len(new_items) - 10}건")
    lines.extend([
        "",
        f"모니터링 URL: {target_url}",
        f"감지 시각(UTC): {utc_now_iso()}",
    ])
    return subject, "\n".join(lines)


def send_ntfy(subject: str, body: str) -> None:
    if not NTFY_TOPIC:
        return
    url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
    headers = {
        "Title": subject,
        "Priority": "default",
        "Tags": "books,mag",
    }
    if DRY_RUN:
        log(f"[dry-run][ntfy] POST {url}\n{subject}\n{body}")
        return
    response = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=30)
    response.raise_for_status()
    log(f"[notify] ntfy sent -> {url}")


def send_email(subject: str, body: str) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM and EMAIL_TO):
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    if DRY_RUN:
        log(f"[dry-run][email] To={EMAIL_TO}\n{subject}\n{body}")
        return

    if SMTP_PORT == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

    log(f"[notify] email sent -> {EMAIL_TO}")


def send_telegram(subject: str, body: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": f"{subject}\n\n{body}",
        "disable_web_page_preview": True,
    }

    if DRY_RUN:
        log(f"[dry-run][telegram] POST {url}\n{payload['text']}")
        return

    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    log("[notify] telegram sent")


def notify_all(subject: str, body: str) -> None:
    if not any([NTFY_TOPIC, EMAIL_TO, TELEGRAM_CHAT_ID]):
        log("[warn] No notification channel configured. Set NTFY_TOPIC and/or SMTP_* and/or TELEGRAM_*.")
        return
    send_ntfy(subject, body)
    send_email(subject, body)
    send_telegram(subject, body)


def validate_config() -> None:
    if not TARGET_URL:
        raise SystemExit(
            textwrap.dedent(
                """
                TARGET_URL is required.

                Example:
                  TARGET_URL="https://theolib.cu.ac.kr/newarrival"

                Best practice for DCU:
                1) Open the public new-arrivals page in your browser.
                2) Filter once to '신학도서관'.
                3) Copy the final results-page URL from the address bar.
                4) Put that URL into TARGET_URL.
                """
            ).strip()
        )


def coerce_items(raw_items: list[dict[str, Any]]) -> list[Item]:
    items: list[Item] = []
    for entry in raw_items:
        try:
            items.append(
                Item(
                    item_id=str(entry["item_id"]),
                    title=str(entry["title"]),
                    link=str(entry.get("link", "")),
                    block_text=str(entry.get("block_text", "")),
                )
            )
        except KeyError:
            continue
    return items


def main() -> int:
    validate_config()
    ensure_dirs()

    state = load_state()
    previous_items = coerce_items(state.get("seen_items", []))

    log(f"[config] TARGET_URL={TARGET_URL}")
    if INCLUDE_TEXT:
        log(f"[config] INCLUDE_TEXT={INCLUDE_TEXT}")

    current_items = browser_extract(TARGET_URL)
    log(f"[result] extracted {len(current_items)} candidate items")

    if not previous_items:
        log("[init] first run detected; saving baseline without sending notifications")
        save_state(current_items, state)
        return 0

    new_items = diff_items(previous_items, current_items)
    log(f"[diff] new items: {len(new_items)}")

    if new_items:
        subject, body = format_notification(new_items, TARGET_URL)
        notify_all(subject, body)
    else:
        log("[diff] no new items")

    save_state(current_items, state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MonitorError as exc:
        log(f"[error] {exc}")
        raise SystemExit(1)

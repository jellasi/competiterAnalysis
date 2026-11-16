#!/usr/bin/env python
"""Weekly competitor monitor for SaveTax refund market.

- Monitors homepage / notices / terms / app store pages.
- Sends alerts to Slack Incoming Webhook and/or SMTP email.
- Designed for GitHub Actions scheduled runs.
- Uses only Python standard library.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "sources.json"
DEFAULT_STATE = ROOT / "state" / "competitor_state.json"
DEFAULT_REPORT = ROOT / "last_report.md"
DEFAULT_HTML_REPORT = ROOT / "last_report.html"
DEFAULT_SLACK_REPORT = ROOT / "last_slack_message.txt"
DEFAULT_SLACK_THREAD = ROOT / "last_slack_thread.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 SaveTaxCompetitorMonitor/1.0"
)

NOISE_PATTERNS = [
    r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b",
    r"\b\d{1,2}:\d{2}(:\d{2})?\b",
    r"조회수\s*\d+",
    r"updated\s+\d+\s+(seconds?|minutes?|hours?|days?)\s+ago",
    r"csrf[-_a-zA-Z0-9]*",
]


@dataclass
class Fetched:
    ok: bool
    source_id: str
    company: str
    label: str
    importance: str
    url: str
    title: str = ""
    text: str = ""
    normalized_text: str = ""
    digest: str = ""
    metadata: dict[str, Any] | None = None
    error: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def http_get(url: str, timeout: int = 30) -> tuple[str, str]:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.4",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        content_type = resp.headers.get("Content-Type", "")
    charset = "utf-8"
    m = re.search(r"charset=([^;]+)", content_type, re.I)
    if m:
        charset = m.group(1).strip()
    try:
        return raw.decode(charset, errors="replace"), content_type
    except LookupError:
        return raw.decode("utf-8", errors="replace"), content_type


def extract_title(raw: str) -> str:
    candidates = [
        r"<title[^>]*>(.*?)</title>",
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pat in candidates:
        m = re.search(pat, raw, flags=re.I | re.S)
        if m:
            return clean_spaces(html.unescape(strip_tags(m.group(1))))[:180]
    return ""


def extract_meta_content(raw: str, name: str) -> str:
    patterns = [
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, raw, flags=re.I | re.S)
        if m:
            return clean_spaces(html.unescape(m.group(1)))
    return ""


def strip_tags(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|canvas|iframe).*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</(p|div|li|h[1-6]|section|article|tr)>", "\n", raw)
    raw = re.sub(r"(?is)<[^>]+>", " ", raw)
    return html.unescape(raw)


def clean_spaces(text: str) -> str:
    text = text.replace("\u00a0", " ")
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def normalize_text(text: str) -> str:
    text = clean_spaces(text).lower()
    for pat in NOISE_PATTERNS:
        text = re.sub(pat, " ", text, flags=re.I)
    # Keep meaningful Korean/English/numeric text, normalize repeated whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) >= 2]
    # Deduplicate consecutive repeated lines, common in app pages.
    deduped = []
    prev = None
    for line in lines:
        if line != prev:
            deduped.append(line)
        prev = line
    return "\n".join(deduped)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def extract_google_play_summary(raw: str) -> tuple[str, dict[str, Any]]:
    """Return a stable subset of a Google Play app page.

    Full Play Store pages include rotating reviews/recommendations. Those create
    false weekly alerts, so keep only app-level fields useful for update checks.
    """
    title = extract_title(raw)
    description = extract_meta_content(raw, "description")
    text = clean_spaces(strip_tags(raw))
    lines = text.splitlines()

    keep: list[str] = []
    labels = {
        "새로운 기능",
        "업데이트 날짜",
        "버전",
        "필요한 Android 버전",
        "다운로드",
        "콘텐츠 등급",
        "제공자",
        "개발자",
        "개인정보처리방침",
    }
    stop_markers = {"리뷰", "리뷰 모두 보기", "앱 정보", "데이터 보안", "평점 및 리뷰", "flag 부적절한 앱으로 신고", "개발자", "google store", "모두 vat 포함된 가격입니다.", "대한민국 (한국어)"}
    for i, line in enumerate(lines):
        if line in labels:
            keep.append(line)
            for nxt in lines[i + 1 : i + 5]:
                if nxt in labels or nxt in stop_markers:
                    break
                keep.append(nxt)

    summary_lines = [f"title: {title}"] if title else []
    if description:
        summary_lines.append(f"description: {description}")
    summary_lines.extend(keep)
    summary = clean_spaces("\n".join(summary_lines))
    metadata = {
        "content_type": "google_play_summary",
        "title": title,
        "description": description,
    }
    return summary, metadata


def fetch_webpage(company: str, source: dict[str, Any]) -> Fetched:
    url = source["url"]
    try:
        raw, content_type = http_get(url)
        title = extract_title(raw)
        if "play.google.com/store/apps/details" in url:
            text, metadata = extract_google_play_summary(raw)
            metadata["length"] = len(raw)
        else:
            text = clean_spaces(strip_tags(raw))
            metadata = {"content_type": content_type, "length": len(raw)}
        normalized = normalize_text(text)
        return Fetched(
            ok=True,
            source_id=source["id"],
            company=company,
            label=source["label"],
            importance=source.get("importance", "medium"),
            url=url,
            title=title,
            text=text[:20000],
            normalized_text=normalized[:40000],
            digest=digest(normalized),
            metadata=metadata,
        )
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        return Fetched(False, source["id"], company, source["label"], source.get("importance", "medium"), url, error=repr(e))


def fetch_appstore(company: str, source: dict[str, Any]) -> Fetched:
    params = urlencode({"id": source["app_id"], "country": source.get("country", "kr"), "lang": "ko_kr"})
    url = f"https://itunes.apple.com/lookup?{params}"
    try:
        raw, _ = http_get(url)
        data = json.loads(raw)
        result = (data.get("results") or [{}])[0]
        fields = {
            "trackName": result.get("trackName"),
            "version": result.get("version"),
            "currentVersionReleaseDate": result.get("currentVersionReleaseDate"),
            "releaseNotes": result.get("releaseNotes"),
            "description": result.get("description"),
            "sellerName": result.get("sellerName"),
            "trackViewUrl": result.get("trackViewUrl"),
        }
        text = "\n".join(f"{k}: {v}" for k, v in fields.items() if v)
        normalized = normalize_text(text)
        return Fetched(
            ok=True,
            source_id=source["id"],
            company=company,
            label=source["label"],
            importance=source.get("importance", "medium"),
            url=fields.get("trackViewUrl") or url,
            title=fields.get("trackName") or source["label"],
            text=text,
            normalized_text=normalized,
            digest=digest(normalized),
            metadata=fields,
        )
    except Exception as e:  # noqa: BLE001 - keep monitor resilient
        return Fetched(False, source["id"], company, source["label"], source.get("importance", "medium"), url, error=repr(e))


def fetch_zendesk_articles(company: str, source: dict[str, Any]) -> Fetched:
    url = source["url"]
    try:
        raw, _ = http_get(url)
        data = json.loads(raw)
        articles = data.get("articles") or []
        rows: list[str] = []
        for article in articles[: int(source.get("limit", 30))]:
            title = article.get("title") or ""
            updated_at = article.get("updated_at") or article.get("created_at") or ""
            html_url = article.get("html_url") or ""
            body = clean_spaces(strip_tags(article.get("body") or ""))
            rows.append(f"title: {title}\nupdated_at: {updated_at}\nurl: {html_url}\nbody: {body[:1200]}")
        text = "\n\n---\n\n".join(rows)
        normalized = normalize_text(text)
        return Fetched(
            ok=True,
            source_id=source["id"],
            company=company,
            label=source["label"],
            importance=source.get("importance", "high"),
            url=url,
            title=source["label"],
            text=text,
            normalized_text=normalized,
            digest=digest(normalized),
            metadata={"content_type": "zendesk_articles", "count": len(articles)},
        )
    except Exception as e:  # noqa: BLE001 - keep monitor resilient
        return Fetched(False, source["id"], company, source["label"], source.get("importance", "high"), url, error=repr(e))


def fetch_all(config: dict[str, Any]) -> list[Fetched]:
    fetched: list[Fetched] = []
    for company_cfg in config["companies"]:
        company = company_cfg["name"]
        for source in company_cfg["sources"]:
            if source["type"] == "webpage":
                item = fetch_webpage(company, source)
            elif source["type"] == "appstore_lookup":
                item = fetch_appstore(company, source)
            elif source["type"] == "zendesk_articles":
                item = fetch_zendesk_articles(company, source)
            else:
                item = Fetched(False, source["id"], company, source["label"], source.get("importance", "medium"), source.get("url", ""), error=f"unknown source type: {source['type']}")
            fetched.append(item)
            time.sleep(0.7)  # polite pacing
    return fetched


def state_record(item: Fetched) -> dict[str, Any]:
    return {
        "company": item.company,
        "label": item.label,
        "importance": item.importance,
        "url": item.url,
        "title": item.title,
        "digest": item.digest,
        "normalized_text": item.normalized_text,
        "metadata": item.metadata or {},
        "ok": item.ok,
        "error": item.error,
        "last_seen_at": now_iso(),
    }


def classify_change(item: Fetched, old: dict[str, Any] | None, config: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    severity = item.importance
    combined = "\n".join([item.title, item.normalized_text[:5000]])
    for kw in config.get("keywords", {}).get("high", []):
        if kw.lower() in combined.lower():
            severity = "high"
            reasons.append(f"중요 키워드 감지: {kw}")
            break
    if severity != "high":
        for kw in config.get("keywords", {}).get("medium", []):
            if kw.lower() in combined.lower():
                severity = "medium"
                reasons.append(f"관심 키워드 감지: {kw}")
                break
    if old and old.get("metadata") != (item.metadata or {}) and item.metadata:
        reasons.append("버전/릴리즈노트 등 메타데이터 변경")
        if item.importance == "medium":
            severity = "medium"
    if old and old.get("title") != item.title:
        reasons.append("페이지 제목 변경")
    return severity, reasons


def build_diff(old_text: str, new_text: str, max_lines: int = 80) -> str:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile="previous", tofile="current", lineterm=""))
    if len(diff_lines) > max_lines:
        head = diff_lines[: max_lines // 2]
        tail = diff_lines[-max_lines // 2 :]
        diff_lines = head + [f"... diff truncated; {len(diff_lines) - max_lines} lines omitted ..."] + tail
    return "\n".join(diff_lines)


def detect_changes(fetched: list[Fetched], previous: dict[str, Any], config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, str]]]:
    changes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    new_sources: dict[str, Any] = {}
    old_sources: dict[str, Any] = previous.get("sources", {})

    for item in fetched:
        new_sources[item.source_id] = state_record(item)
        if not item.ok:
            errors.append({"company": item.company, "label": item.label, "url": item.url, "error": item.error})
            continue
        old = old_sources.get(item.source_id)
        if not old:
            continue  # baseline only; avoid first-run noise
        if old.get("digest") != item.digest:
            severity, reasons = classify_change(item, old, config)
            changes.append(
                {
                    "company": item.company,
                    "label": item.label,
                    "url": item.url,
                    "severity": severity,
                    "title_before": old.get("title", ""),
                    "title_after": item.title,
                    "reasons": reasons,
                    "metadata_before": old.get("metadata", {}),
                    "metadata_after": item.metadata or {},
                    "diff": build_diff(old.get("normalized_text", ""), item.normalized_text),
                }
            )

    new_state = {
        "updated_at": now_iso(),
        "sources": new_sources,
    }
    return changes, new_state, errors


def md_escape(s: Any) -> str:
    return str(s or "").strip()


def format_report(changes: list[dict[str, Any]], errors: list[dict[str, str]], previous_existed: bool) -> str:
    date_kst_hint = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 세이브택스 환급 주간 경쟁사 변경 취합 리포트", "", f"- 실행 시각: {date_kst_hint}", "- 정기 발송: 매주 월요일 오전 8시(KST)", f"- 감지 변경: {len(changes)}건", f"- 수집 오류: {len(errors)}건", ""]
    if not previous_existed:
        lines += ["> 첫 실행이라 기준 스냅샷만 저장했습니다. 다음 실행부터 변경사항을 알립니다.", ""]
    if changes:
        lines.append("## 변경 감지")
        lines.append("")
        severity_order = {"high": 0, "medium": 1, "low": 2}
        for ch in sorted(changes, key=lambda x: (severity_order.get(x["severity"], 9), x["company"], x["label"])):
            lines += [
                f"### [{ch['severity'].upper()}] {ch['company']} - {ch['label']}",
                "",
                f"- URL: {ch['url']}",
            ]
            if ch.get("title_before") != ch.get("title_after"):
                lines += [f"- 제목 변경: `{md_escape(ch.get('title_before'))}` → `{md_escape(ch.get('title_after'))}`"]
            if ch.get("reasons"):
                lines += ["- 감지 사유: " + ", ".join(ch["reasons"])]
            before = ch.get("metadata_before") or {}
            after = ch.get("metadata_after") or {}
            interesting_keys = ["version", "currentVersionReleaseDate", "releaseNotes", "trackName", "sellerName"]
            meta_lines = []
            for key in interesting_keys:
                if before.get(key) != after.get(key) and (before.get(key) or after.get(key)):
                    b = textwrap.shorten(str(before.get(key, "")), width=180, placeholder="...")
                    a = textwrap.shorten(str(after.get(key, "")), width=180, placeholder="...")
                    meta_lines.append(f"  - {key}: `{b}` → `{a}`")
            if meta_lines:
                lines.append("- 앱/메타 정보 변경:")
                lines.extend(meta_lines)
            diff = ch.get("diff") or ""
            if diff:
                lines += ["", "```diff", diff[:6000], "```"]
            lines.append("")
    else:
        lines += ["## 변경 감지", "", "이번 실행에서 의미 있는 변경사항은 감지되지 않았습니다.", ""]

    if errors:
        lines += ["## 수집 오류", ""]
        for err in errors:
            lines.append(f"- {err['company']} / {err['label']}: {err['error']} ({err['url']})")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def should_notify(changes: list[dict[str, Any]], errors: list[dict[str, str]], notify_on_errors: bool) -> bool:
    return bool(changes) or (notify_on_errors and bool(errors))


def send_slack(report: str, thread: list[str] | None = None) -> None:
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    channel_id = os.getenv("SLACK_CHANNEL_ID", "").strip()
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()

    text = report
    if len(text) > 3800:
        text = text[:3800] + "\n...보고서가 길어 일부 생략되었습니다. 상세 리포트를 확인하세요."

    # Preferred path: post as the installed TA bot via Slack Web API.
    # Required secrets: SLACK_BOT_TOKEN + SLACK_CHANNEL_ID.
    if bot_token and channel_id:
        payload = json.dumps({"channel": channel_id, "text": text, "unfurl_links": False, "unfurl_media": False}).encode("utf-8")
        req = Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 300:
                raise RuntimeError(f"Slack bot API failed: HTTP {resp.status}")
            data = json.loads(body)
            if not data.get("ok"):
                raise RuntimeError(f"Slack bot API failed: {data.get('error', 'unknown_error')}")
        print("Slack bot notification sent")
        parent_ts = data.get("ts")
        for reply in (thread or []):
            post_slack_bot(bot_token, channel_id, reply[:3800], parent_ts)
            time.sleep(1.1)
        if thread:
            print(f"Slack thread replies sent: {len(thread)}")
        return

    # Backward-compatible fallback: Incoming Webhook.
    if webhook:
        payload = json.dumps({"text": text}).encode("utf-8")
        req = Request(webhook, data=payload, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
        with urlopen(req, timeout=20) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Slack webhook failed: HTTP {resp.status}")
        print("Slack webhook notification sent")
        return

    print("SLACK_BOT_TOKEN/SLACK_CHANNEL_ID or SLACK_WEBHOOK_URL not set; skip Slack notification")


def post_slack_bot(bot_token: str, channel_id: str, text: str, thread_ts: str | None = None) -> dict[str, Any]:
    body_obj: dict[str, Any] = {"channel": channel_id, "text": text, "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        body_obj["thread_ts"] = thread_ts
    req = Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(body_obj).encode("utf-8"),
        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not data.get("ok"):
        raise RuntimeError(f"Slack bot API failed: {data.get('error', 'unknown_error')}")
    return data


def send_email(report: str, html_report: str | None = None) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    mail_from = os.getenv("EMAIL_FROM", username).strip()
    mail_to = os.getenv("EMAIL_TO", "").strip()
    if not host or not mail_to or not mail_from:
        print("SMTP_HOST/EMAIL_TO/EMAIL_FROM not fully set; skip email notification")
        return
    port = int(os.getenv("SMTP_PORT") or "587")
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}

    msg = EmailMessage()
    msg["Subject"] = "[세이브택스] 주간 경쟁사 변경 취합 리포트"
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(report)
    if html_report:
        msg.add_alternative(html_report, subtype="html")

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
            if username or password:
                smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if username or password:
                smtp.login(username, password)
            smtp.send_message(msg)
    print("Email notification sent")




def parse_date_start(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def parse_date_end_exclusive(value: str) -> datetime:
    return parse_date_start(value) + timedelta(days=1)


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_korean_date_line(value: str) -> datetime | None:
    m = re.search(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", value or "")
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    return datetime(y, mo, d, tzinfo=timezone.utc)


def in_period(dt: datetime | None, start: datetime, end_exclusive: datetime) -> bool:
    return bool(dt and start <= dt < end_exclusive)


def summarize_body(text: str, limit: int = 420) -> str:
    return textwrap.shorten(clean_spaces(strip_tags(text or "")), width=limit, placeholder="...")


def collect_zendesk_period(company: str, source: dict[str, Any], start: datetime, end_exclusive: datetime) -> list[dict[str, Any]]:
    url = source["url"]
    # Ask Zendesk for a larger first page; recent items are enough for weekly checks.
    sep = "&" if "?" in url else "?"
    if "per_page=" not in url:
        url = f"{url}{sep}per_page=100"
    raw, _ = http_get(url)
    data = json.loads(raw)
    rows = []
    for article in data.get("articles") or []:
        # updated_at 은 라벨·순서만 바꿔도 바뀐다(예: 1월 공지가 11월에 updated_at 만 갱신).
        # 본문이 실제로 바뀐 시각인 edited_at, 없으면 created_at 으로 기간을 거른다.
        created = parse_iso_datetime(article.get("created_at") or "")
        edited = parse_iso_datetime(article.get("edited_at") or "") or created
        if not in_period(edited, start, end_exclusive):
            continue
        is_new = in_period(created, start, end_exclusive)
        body_text = clean_spaces(strip_tags(article.get("body") or ""))
        rows.append({
            "company": company,
            "source": source["label"],
            "source_type": "notice",
            "kind": "새 공지" if is_new else "공지 본문 수정",
            "date": edited.isoformat() if edited else "",
            "created": created.isoformat() if created else "",
            "title": article.get("title") or "",
            "url": article.get("html_url") or url,
            "summary": summarize_body(body_text),
            "body": body_text[:8000],
            "facts": extract_notice_facts(body_text),
        })
    return rows


# 본문에 있으면 사람이 꼭 봐야 하는 말(돈·동의·서비스 지속). 분류·중요도·요약에 함께 쓴다.
WATCH_TERMS = [
    "이용료", "수수료", "자동출금", "자동 출금", "CMS", "환불", "가격", "요금", "할인",
    "환급 완료", "서비스 종료", "서비스 중단", "개인정보", "제3자", "처리방침", "위탁", "동의",
]
MONEY_TERMS = ["이용료", "수수료", "자동출금", "자동 출금", "CMS", "환불", "가격", "요금", "환급 완료"]
PRIVACY_TERMS = ["개인정보", "제3자", "처리방침", "위탁"]
STOP_TERMS = ["서비스 종료", "서비스 중단"]


def extract_notice_facts(text: str) -> dict[str, Any]:
    """공지 본문에서 공고일·시행일과 바뀐 조항 줄을 뽑는다(사람이 바로 읽을 핵심)."""
    facts: dict[str, Any] = {}
    date_pat = r"(\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일|\d{4}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{1,2})"
    m = re.search(r"공고\s*일(?:자)?\s*[:：]?\s*" + date_pat, text)
    if m:
        facts["announced"] = re.sub(r"\s+", " ", m.group(1))
    m = re.search(r"시행\s*일(?:자)?\s*[:：]?\s*" + date_pat, text)
    if m:
        facts["effective"] = re.sub(r"\s+", " ", m.group(1))
    clauses: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*제\s*\d+\s*조", line):
            detail = [line.strip()]
            for nxt in lines[i + 1 : i + 4]:
                if re.match(r"^\s*(제\s*\d+\s*조|\[|※|감사합니다)", nxt):
                    break
                detail.append(nxt.strip().lstrip("-·• ").strip())
            clauses.append(textwrap.shorten(" ".join(d for d in detail if d), width=260, placeholder="..."))
        if len(clauses) >= 5:
            break
    if clauses:
        facts["clauses"] = clauses
    hits = [kw for kw in WATCH_TERMS if kw.lower() in text.lower()]
    if hits:
        facts["hits"] = hits
    return facts


def collect_appstore_period(company: str, source: dict[str, Any], start: datetime, end_exclusive: datetime) -> list[dict[str, Any]]:
    item = fetch_appstore(company, source)
    if not item.ok:
        return []
    meta = item.metadata or {}
    dt = parse_iso_datetime(meta.get("currentVersionReleaseDate") or "")
    if not in_period(dt, start, end_exclusive):
        return []
    notes = meta.get("releaseNotes") or ""
    return [{
        "company": company,
        "source": source["label"],
        "source_type": "app",
        "platform": "iOS",
        "version": meta.get("version") or "",
        "app_name": app_display_name(meta.get("trackName") or item.title),
        "kind": "앱 업데이트",
        "date": dt.isoformat() if dt else "",
        "title": f"{meta.get('trackName') or item.title} v{meta.get('version') or ''}".strip(),
        "url": meta.get("trackViewUrl") or item.url,
        "summary": summarize_body(notes) if notes else "릴리즈노트 없음",
        "body": notes,
    }]


def collect_google_play_period(company: str, source: dict[str, Any], start: datetime, end_exclusive: datetime) -> list[dict[str, Any]]:
    item = fetch_webpage(company, source)
    if not item.ok:
        return []
    lines = item.normalized_text.splitlines()
    update_date = None
    release_notes = []
    stop_markers = {"업데이트 날짜", "flag 부적절한 앱으로 신고", "개발자", "google store", "모두 vat 포함된 가격입니다.", "대한민국 (한국어)", "앱 지원", "개발자 소개"}
    for idx, line in enumerate(lines):
        if line == "업데이트 날짜" and idx + 1 < len(lines):
            update_date = parse_korean_date_line(lines[idx + 1])
        if line == "새로운 기능":
            release_notes = []
            for nxt in lines[idx + 1: idx + 12]:
                if nxt in stop_markers:
                    break
                release_notes.append(nxt)
    if not in_period(update_date, start, end_exclusive):
        return []
    notes = "\n".join(release_notes)
    return [{
        "company": company,
        "source": source["label"],
        "source_type": "app",
        "platform": "Android",
        "version": "",
        "app_name": app_display_name(item.title or source["label"]),
        "kind": "앱 업데이트",
        "date": update_date.isoformat() if update_date else "",
        "title": item.title or source["label"],
        "url": item.url,
        "summary": summarize_body(notes) if notes else "릴리즈노트 없음",
        "body": notes,
    }]


def app_display_name(title: str) -> str:
    """'덧셈 - 세금 신고/환급 - Google Play 앱' 과 '덧셈 - 세금 신고/환급' 을 같은 이름으로."""
    name = re.sub(r"\s*-\s*Google Play\s*앱\s*$", "", title or "").strip()
    name = re.sub(r"\s+v?\d+(\.\d+)+\s*$", "", name).strip()
    return name.split(" - ")[0].strip() or name


def report_url() -> str:
    explicit = os.getenv("REPORT_URL", "").strip()
    if explicit:
        return explicit
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "jellasi/competiterAnalysis").strip()
    run_id = os.getenv("GITHUB_RUN_ID", "").strip()
    if run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return f"{server}/{repo}/actions"


def html_report_url() -> str:
    explicit = os.getenv("HTML_REPORT_URL", "").strip()
    if explicit:
        return explicit
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "jellasi/competiterAnalysis").strip()
    blob_url = f"{server}/{repo}/blob/main/last_report.html"
    return "https://htmlpreview.github.io/?" + blob_url


COMPETITORS = ["삼쩜삼", "덧셈컴퍼니", "비즈넵 환급"]
SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
SEVERITY_ICON = {"HIGH": "🚨", "MEDIUM": "⚠️", "LOW": "ℹ️"}
SEVERITY_KO = {"HIGH": "높음", "MEDIUM": "보통", "LOW": "낮음"}
NEW_FEATURE_TERMS = ["새로운", "신규", "출시", "추가", "오픈", "도입", "새 기능", "런칭"]
PROMO_TERMS = ["프로모션", "이벤트", "캠페인", "쿠폰", "무료"]


def row_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(k, "")) for k in ["kind", "title", "summary", "source", "body"])


def has_any(text: str, terms: list[str]) -> list[str]:
    low = (text or "").lower()
    return [t for t in terms if t.lower() in low]


def classify_ci_row(row: dict[str, Any]) -> tuple[str, str]:
    """구분·중요도. 돈·종료·개인정보 같은 '사람이 꼭 볼 말'을 콘텐츠/마케팅보다 먼저 본다."""
    stype = row.get("source_type", "")
    title_src = f"{row.get('title', '')} {row.get('source', '')}"
    text = f"{row.get('title', '')}\n{row.get('body') or row.get('summary', '')}"
    if stype == "app":
        notes = row.get("body") or ""
        if has_any(notes, MONEY_TERMS + STOP_TERMS):
            return "제품(앱)", "MEDIUM"
        if has_any(notes, NEW_FEATURE_TERMS):
            return "제품(앱)", "MEDIUM"
        return "제품(앱)", "LOW"
    if has_any(text, STOP_TERMS):
        return "서비스 종료·중단", "HIGH"
    if has_any(text, MONEY_TERMS):
        return "요금·결제", "HIGH"
    if has_any(text, PRIVACY_TERMS):
        return "개인정보·약관", "MEDIUM"
    if has_any(title_src, ["약관", "이용규칙", "정책"]):
        return "약관", "MEDIUM"
    if has_any(text, PROMO_TERMS) or "할인" in text:
        return "프로모션", "MEDIUM"
    if stype == "page":
        return "홈페이지 문구", "LOW"
    return "콘텐츠·일반 공지", "LOW"


def impact_for(row: dict[str, Any], category: str, severity: str) -> str:
    c = row.get("company", "경쟁사")
    if category == "요금·결제" and row.get("source_type") == "page":
        return (f"{c} 홈페이지의 요금·할인·환급 관련 문구가 바뀌었습니다. 고객에게 내세우는 조건이 달라졌을 수 있어 "
                "세이브택스 환급의 수수료·혜택 안내와 비교가 필요합니다.")
    if category == "요금·결제":
        return (f"{c}에서 이용료·결제 조건을 바꿨습니다. 고객이 두 서비스를 비교할 때 바로 보는 조건이라, "
                "세이브택스 환급의 수수료율·출금 시점·환불 기준과 나란히 놓고 차이를 확인해야 합니다.")
    if category == "서비스 종료·중단":
        return f"{c} 서비스 일부가 종료·중단됩니다. 해당 고객이 대체 서비스를 찾는 시점이라 유입 기회가 될 수 있습니다."
    if category == "개인정보·약관":
        return f"{c}의 개인정보 제공·위탁 범위가 바뀌었습니다. 세이브택스 환급의 동의 화면·고지 문구와 차이를 확인해야 합니다."
    if category == "약관":
        return f"{c} 약관이 바뀌었습니다. 바뀐 조항이 고객 책임·환불·서비스 범위에 닿는지 확인이 필요합니다."
    if category == "프로모션":
        return f"{c}에서 할인·이벤트를 시작했습니다. 같은 기간 우리 유입·전환이 흔들리는지 지표를 함께 보세요."
    if category == "제품(앱)" and severity != "LOW":
        return f"{c} 앱에 새 기능 또는 조건 변경이 들어갔습니다. 환급 조회·신청 흐름에 닿는 기능인지 실제 앱에서 확인이 필요합니다."
    if category == "제품(앱)":
        return f"{c} 앱의 작은 개선·오류 수정입니다. 릴리즈노트만으로는 바뀐 화면을 알 수 없어 영향은 작게 봅니다."
    if category == "홈페이지 문구":
        return f"{c} 홈페이지 문구가 바뀌었습니다. 내세우는 혜택·숫자가 달라졌는지 바뀐 줄을 확인하세요."
    return f"{c}의 일반 공지·콘텐츠입니다. 직접 영향은 작고 참고용입니다."


def action_for(row: dict[str, Any], category: str, severity: str) -> str:
    c = row.get("company", "경쟁사")
    if category == "요금·결제" and row.get("source_type") == "page":
        return f"{c} 홈페이지 바뀐 요금·혜택 문구 캡처, 우리 안내와 비교"
    if category == "요금·결제":
        return f"{c} 공지의 바뀐 조항을 우리 수수료·출금·환불 정책과 표로 비교"
    if category == "서비스 종료·중단":
        return f"{c} 종료 대상 고객층·시점 확인, 우리 안내 문구·유입 채널 검토"
    if category == "개인정보·약관":
        return f"{c} 처리방침 바뀐 조항과 우리 동의·제3자 제공 고지 비교"
    if category == "약관":
        return f"{c} 약관 바뀐 조항 전·후 비교"
    if category == "프로모션":
        return f"{c} 프로모션 조건·기간 기록, 같은 기간 유입 지표 확인"
    if category == "제품(앱)" and severity != "LOW":
        return f"{c} 최신 앱 설치 후 새 기능 화면 캡처"
    if category == "홈페이지 문구":
        return f"{c} 홈페이지 바뀐 문구 캡처·기록"
    return "조치 불필요(관찰만)"


def short_date(value: str) -> str:
    return (value or "")[:10]


def date_text(row: dict[str, Any]) -> str:
    stype = row.get("source_type")
    if stype == "notice":
        if row.get("kind") == "새 공지":
            return f"게시 {short_date(row.get('date'))}"
        return f"본문 수정 {short_date(row.get('date'))} (첫 게시 {short_date(row.get('created'))})"
    if stype == "app":
        return f"업데이트 {short_date(row.get('date'))}"
    if stype == "page":
        base = short_date(row.get("baseline_at")) or "확인 필요"
        return f"확인 {short_date(row.get('date'))} (직전 저장본 {base} 대비)"
    return short_date(row.get("date"))


def fact_lines(row: dict[str, Any], limit: int = 3) -> list[str]:
    """사람이 바로 읽을 '무엇이 바뀌었나' 줄."""
    out: list[str] = []
    facts = row.get("facts") or {}
    if facts.get("announced") or facts.get("effective"):
        out.append(" · ".join(x for x in [
            f"공고 {facts['announced']}" if facts.get("announced") else "",
            f"시행 {facts['effective']}" if facts.get("effective") else "",
        ] if x))
    for clause in (facts.get("clauses") or [])[:limit]:
        out.append(clause)
    diff = row.get("diff") or {}
    for line in (diff.get("added") or [])[:limit]:
        out.append(f"+ {line}")
    for line in (diff.get("removed") or [])[: max(1, limit - 1)]:
        out.append(f"− {line}")
    if row.get("source_type") == "app":
        out.append(f"릴리즈노트: {textwrap.shorten(row.get('summary') or '', width=160, placeholder='...')}")
    if not out:
        out.append(textwrap.shorten(row.get("summary") or "세부 내용은 출처 확인 필요", width=200, placeholder="..."))
    return out


def evidence_line(row: dict[str, Any], check_date: str) -> str:
    return f"{row.get('url')} (확인일: {check_date})"


def build_competitor_data(config: dict[str, Any], date_from: str, date_to: str, state: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[str], list[str], dict[str, Any]]:
    start = parse_date_start(date_from)
    end_exclusive = parse_date_end_exclusive(date_to)
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    errors: list[str] = []
    old_sources = (state or {}).get("sources", {})
    new_sources: dict[str, Any] = dict(old_sources)
    check_date = datetime.now(timezone.utc).date().isoformat()

    for company_cfg in config["companies"]:
        company = company_cfg["name"]
        for source in company_cfg["sources"]:
            try:
                collected: list[dict[str, Any]] = []
                if source["type"] == "zendesk_articles":
                    collected = collect_zendesk_period(company, source, start, end_exclusive)
                elif source["type"] == "appstore_lookup":
                    collected = collect_appstore_period(company, source, start, end_exclusive)
                elif source["type"] == "webpage" and "play.google.com/store/apps/details" in source.get("url", ""):
                    collected = collect_google_play_period(company, source, start, end_exclusive)
                elif source["type"] == "webpage":
                    collected, record, note = collect_page_change(company, source, old_sources.get(source["id"]), check_date)
                    if record:
                        new_sources[source["id"]] = record
                    if note:
                        notes.append(note)
                rows.extend(collected)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{company} / {source['label']}: {e!r}")
            time.sleep(1.0)  # 사이트별 요청 간격 1초 이상

    rows.sort(key=lambda r: (r.get("date", ""), r.get("company", ""), r.get("source", "")))
    new_state = {"updated_at": now_iso(), "sources": new_sources}
    return rows, notes, errors, new_state


def looks_broken(text: str) -> bool:
    """예전 실행(인코딩 깨짐)으로 저장된 기준본은 비교에 쓰지 않는다."""
    return not text or "�" in text[:3000] or len(text) < 40


def collect_page_change(company: str, source: dict[str, Any], old: dict[str, Any] | None, check_date: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str]:
    """날짜가 없는 홈페이지·약관 페이지는 직전 저장본과 줄 단위로 비교한다."""
    item = fetch_webpage(company, source)
    if not item.ok:
        raise RuntimeError(item.error)
    record = state_record(item)
    if len(item.normalized_text) < 40:
        return [], record, f"{company} / {source['label']}: 화면을 스크립트로 그려 본문을 읽을 수 없음(비교 불가)"
    old_text = (old or {}).get("normalized_text", "")
    if not old or looks_broken(old_text):
        return [], record, f"{company} / {source['label']}: 비교 기준을 새로 저장함(다음 주부터 문구 변경 감지)"
    if old.get("digest") == item.digest:
        return [], record, ""
    old_set = set(old_text.splitlines())
    new_lines = item.normalized_text.splitlines()
    new_set = set(new_lines)
    added = [l for l in new_lines if l not in old_set and len(l) >= 6]
    removed = [l for l in old_text.splitlines() if l not in new_set and len(l) >= 6]
    if not added and not removed:
        return [], record, ""
    title = f"{source['label']} 문구 변경 (+{len(added)}줄 / −{len(removed)}줄)"
    if len(added) + len(removed) > 60:
        title = f"{source['label']} 대규모 변경 (+{len(added)}줄 / −{len(removed)}줄, 개편 가능성)"
    row = {
        "company": company,
        "source": source["label"],
        "source_type": "page",
        "kind": "페이지 문구 변경",
        "date": check_date,
        "baseline_at": old.get("last_seen_at", ""),
        "title": title,
        "url": item.url,
        "summary": textwrap.shorten(" / ".join(added[:3] or removed[:3]), width=300, placeholder="..."),
        "body": "\n".join(added + removed),
        "diff": {"added": [textwrap.shorten(l, 140, placeholder="...") for l in added[:8]],
                 "removed": [textwrap.shorten(l, 140, placeholder="...") for l in removed[:8]]},
    }
    hits = has_any(row["body"], WATCH_TERMS)
    if hits:
        row["facts"] = {"hits": hits}
    return [row], record, ""


def merge_key(row: dict[str, Any]) -> tuple[str, str, str]:
    title = re.sub(r"\s+", " ", row.get("title", "")).strip().lower()
    return (row.get("company", ""), row.get("source_type", ""), title)


def aggregate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 앱의 iOS·Android 업데이트를 한 줄로 합치고, 구분·중요도를 붙인다."""
    merged: list[dict[str, Any]] = []
    apps: dict[tuple[str, str, str], dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        row = dict(row)
        if row.get("source_type") == "app":
            # 같은 주에 같은 앱이 iOS·Android 둘 다 올라오면 한 줄로(릴리즈노트가 다르면 둘 다 남김).
            key = (row.get("company", ""), row.get("app_name", ""), "")
            label = f"{row['platform']} v{row['version']}" if row.get("version") else row.get("platform", "")
            if key in apps:
                ex = apps[key]
                ex["platforms"].append(label)
                ex["links"].append((row.get("platform", "링크"), row.get("url", "")))
                ex["date"] = max(ex.get("date", ""), row.get("date", ""))
                if row.get("summary") and row["summary"] not in ex.get("summary", ""):
                    ex["summary"] = f"{ex.get('summary', '')} / ({row.get('platform')}) {row['summary']}"
                    ex["body"] = f"{ex.get('body', '')}\n{row.get('body', '')}"
                continue
            row["platforms"] = [label]
            row["links"] = [(row.get("platform", "링크"), row.get("url", ""))]
            apps[key] = row
            merged.append(row)
            continue
        key = merge_key(row)
        if key in seen:
            continue
        seen.add(key)
        row["links"] = [("원문", row.get("url", ""))]
        merged.append(row)
    for row in merged:
        if row.get("source_type") == "app":
            row["title"] = f"{row.get('app_name') or row['company']} 앱 업데이트 ({' · '.join(row['platforms'])})"
            row["url"] = " / ".join(u for _, u in row["links"] if u)
        category, severity = classify_ci_row(row)
        row["category"] = category
        row["severity"] = severity
    merged.sort(key=lambda r: (SEVERITY_RANK.get(r["severity"], 9), r.get("company", ""), r.get("date", "")))
    return merged


def executive_summary(rows: list[dict[str, Any]], competitors: list[str]) -> list[str]:
    if not rows:
        return ["이번 기간 확인된 주요 변화는 없습니다."]
    bullets = []
    for row in rows[:3]:
        bullets.append(f"[{SEVERITY_KO[row['severity']]}] {row['company']} · {row['title']} — {impact_for(row, row['category'], row['severity'])}")
    quiet = [c for c in competitors if c not in {r["company"] for r in rows}]
    if quiet:
        bullets.append(f"{', '.join(quiet)}: 이번 기간 감지된 변화 없음.")
    return bullets


def split_urls(value: str) -> list[str]:
    urls: list[str] = []
    for part in re.split(r"\s+/\s+", value or ""):
        part = part.strip()
        if part.startswith("http") and part not in urls:
            urls.append(part)
    return urls


def slack_escape(text: str) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_link(url: str, label: str = "근거") -> str:
    return f"<{url}|{label}>" if url else "근거 확인 필요"


def slack_links(row: dict[str, Any]) -> str:
    links = [slack_link(u, lbl) for lbl, u in row.get("links", []) if u]
    return " · ".join(links) or "근거 확인 필요"


def html_links(row: dict[str, Any]) -> str:
    links = [(lbl, u) for lbl, u in row.get("links", []) if u]
    if not links:
        return '<span class="muted">확인 필요</span>'
    return "".join(f'<a href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(lbl)}</a>' for lbl, u in links)


def badge_class(severity: str) -> str:
    return {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}.get(severity, "low")


def actions_for_rows(rows: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for row in rows:
        if row["severity"] == "LOW":
            continue
        a = action_for(row, row["category"], row["severity"])
        if a not in actions:
            actions.append(a)
    return actions


def build_html_ci_report(rows: list[dict[str, Any]], notes: list[str], errors: list[str], date_from: str, date_to: str) -> str:
    period = f"{date_from} ~ {date_to}"
    check_date = datetime.now(timezone.utc).date().isoformat()
    ordered = aggregate_rows(rows)
    counts = {s: sum(1 for r in ordered if r["severity"] == s) for s in SEVERITY_RANK}

    cards: list[str] = []
    for row in ordered:
        category, severity = row["category"], row["severity"]
        facts = "".join(f"<li>{html.escape(x)}</li>" for x in fact_lines(row, limit=5))
        hits = (row.get("facts") or {}).get("hits") or []
        hits_html = f"<div class='tiny'>감지한 말: {html.escape(', '.join(hits))}</div>" if hits else ""
        cards.append(f"""
        <article class="card">
          <div class="card-head">
            <div>
              <div class="company">{html.escape(row.get('company', ''))}</div>
              <h3>{html.escape(row.get('title', ''))}</h3>
            </div>
            <span class="badge {badge_class(severity)}">{SEVERITY_KO[severity]}</span>
          </div>
          <div class="meta"><span>🏷️ {html.escape(category)}</span><span>📅 {html.escape(date_text(row))}</span><span>📂 {html.escape(row.get('source', ''))}</span></div>
          <div class="block"><strong>무엇이 바뀌었나</strong><ul>{facts}</ul>{hits_html}</div>
          <div class="grid-two">
            <div class="block"><strong>우리에게 미치는 영향</strong><p>{html.escape(impact_for(row, category, severity))}</p></div>
            <div class="block"><strong>권장 대응</strong><p>{html.escape(action_for(row, category, severity))}</p></div>
          </div>
          <div class="sources">🔗 {html_links(row)}</div>
        </article>""")

    competitor_sections: list[str] = []
    for comp in COMPETITORS:
        comp_rows = [r for r in ordered if r.get("company") == comp]
        if comp_rows:
            items = "".join(f"<li>{html.escape(r['title'])} <span class='tiny'>({SEVERITY_KO[r['severity']]} · {html.escape(r['category'])})</span></li>" for r in comp_rows)
            body = f"<ul>{items}</ul>"
        else:
            body = "<p class='muted'>이번 기간 감지된 변화 없음.</p>"
        competitor_sections.append(f"<section class='mini'><h3>{html.escape(comp)}</h3>{body}</section>")

    actions_html = "".join(f"<li>{html.escape(a)}</li>" for a in actions_for_rows(ordered)) or "<li>이번 주 바로 할 일 없음(관찰만)</li>"
    notes_html = "".join(f"<li>{html.escape(x)}</li>" for x in notes) or "<li>없음</li>"
    errors_html = "".join(f"<li>{html.escape(x)}</li>" for x in errors) or "<li>없음</li>"
    cards_html = "".join(cards) if cards else '<div class="card">이번 기간 감지된 변화 없음</div>'

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(period)} 경쟁사 동향 리포트</title>
  <style>
    :root {{ --card:#ffffff; --ink:#172033; --muted:#64748b; --line:#e5e7eb; --blue:#2563eb; --green:#059669; --amber:#d97706; --red:#dc2626; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Pretendard,"Noto Sans KR",Arial,sans-serif; background:#f3f6fb; color:var(--ink); line-height:1.65; }}
    .hero {{ background:linear-gradient(135deg,#0f172a,#1e3a8a); color:#fff; padding:42px 28px; }}
    .wrap {{ max-width:1120px; margin:0 auto; }}
    .hero h1 {{ margin:0 0 12px; font-size:34px; letter-spacing:-.04em; }}
    .hero p {{ margin:0; color:#cbd5e1; }}
    .stats {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:24px; }}
    .stat {{ background:rgba(255,255,255,.12); border:1px solid rgba(255,255,255,.18); padding:12px 16px; border-radius:14px; }}
    main {{ padding:28px; }} section {{ margin:0 0 28px; }} h2 {{ font-size:22px; letter-spacing:-.03em; margin:0 0 14px; }}
    .summary, .mini, .card {{ background:var(--card); border:1px solid var(--line); border-radius:20px; box-shadow:0 12px 28px rgba(15,23,42,.06); }}
    .summary {{ padding:22px 24px; }} .summary li {{ margin:0 0 10px; }}
    .cards {{ display:grid; grid-template-columns:1fr; gap:18px; }} .card {{ padding:22px; }}
    .card-head {{ display:flex; justify-content:space-between; gap:18px; align-items:flex-start; }}
    .company {{ color:var(--blue); font-weight:800; font-size:14px; margin-bottom:4px; }} h3 {{ margin:0; font-size:20px; letter-spacing:-.025em; }}
    .meta {{ display:flex; gap:10px; flex-wrap:wrap; color:var(--muted); margin:12px 0 16px; font-size:14px; }}
    .badge {{ display:inline-flex; align-items:center; border-radius:999px; padding:4px 10px; font-weight:800; font-size:12px; color:#fff; white-space:nowrap; }}
    .badge.high {{ background:var(--red); }} .badge.medium {{ background:var(--amber); }} .badge.low {{ background:var(--green); }}
    .block {{ background:#f8fafc; border:1px solid #edf2f7; border-radius:14px; padding:14px; margin:10px 0; }} .block strong {{ display:block; margin-bottom:6px; }} .block p {{ margin:0; }} .block ul {{ margin:0; padding-left:18px; }}
    .grid-two {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }} .sources {{ margin-top:14px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
    a {{ color:#1d4ed8; font-weight:700; text-decoration:none; background:#eff6ff; padding:4px 8px; border-radius:8px; }}
    .competitors {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }} .mini {{ padding:18px; }} .mini ul {{ padding-left:18px; }} .muted {{ color:var(--muted); }} .tiny {{ color:var(--muted); font-size:12px; }}
    footer {{ color:var(--muted); padding:28px; text-align:center; }} @media (max-width:800px) {{ .grid-two,.competitors {{ grid-template-columns:1fr; }} .hero h1 {{ font-size:28px; }} }}
  </style>
</head>
<body>
  <header class="hero"><div class="wrap">
    <h1>📊 {html.escape(period)} 경쟁사 동향 리포트</h1>
    <p>앱 업데이트 · 공지/약관 본문 수정 · 홈페이지 문구 변경을 세이브택스 환급 관점에서 정리했습니다.</p>
    <div class="stats">
      <div class="stat">🗓️ 작성 기준일<br><strong>{html.escape(check_date)}</strong></div>
      <div class="stat">🏢 분석 대상<br><strong>{' · '.join(COMPETITORS)}</strong></div>
      <div class="stat">🔎 감지<br><strong>{len(ordered)}건 (높음 {counts['HIGH']} · 보통 {counts['MEDIUM']} · 낮음 {counts['LOW']})</strong></div>
    </div>
  </div></header>
  <main class="wrap">
    <section><h2>1. 이번 주 바뀐 것</h2><div class="cards">{cards_html}</div></section>
    <section><h2>2. 경쟁사별 한눈에</h2><div class="competitors">{''.join(competitor_sections)}</div></section>
    <section><h2>3. 확인할 일</h2><div class="summary"><ul>{actions_html}</ul></div></section>
    <section><h2>4. 수집 상태</h2><div class="summary"><strong>참고</strong><ul>{notes_html}</ul><strong>수집 오류</strong><ul>{errors_html}</ul></div></section>
  </main>
  <footer>공지는 본문 수정 시각(edited_at) 기준 · 홈페이지·약관은 직전 저장본과 줄 단위 비교 · 모르는 것은 「확인 필요」로 표시</footer>
</body>
</html>"""


def build_detailed_ci_report(rows: list[dict[str, Any]], notes: list[str], errors: list[str], date_from: str, date_to: str) -> str:
    period = f"{date_from} ~ {date_to}"
    check_date = datetime.now(timezone.utc).date().isoformat()
    ordered = aggregate_rows(rows)
    lines: list[str] = [
        f"# {period} 경쟁사 동향 리포트",
        "",
        "## 리포트 정보",
        f"- 리포트 기간: {period}",
        f"- 작성 기준일: {check_date}",
        f"- 분석 대상 경쟁사: {', '.join(COMPETITORS)}",
        f"- 실행 기록: {report_url()}",
        "",
        "## 1. 요약",
    ]
    lines += [f"- {b}" for b in executive_summary(ordered, COMPETITORS)]
    lines += ["", "## 2. 이번 주 바뀐 것"]
    if not ordered:
        lines += ["- 이번 기간 감지된 변화가 없습니다.", ""]
    for row in ordered:
        category, severity = row["category"], row["severity"]
        lines += [
            f"### [{SEVERITY_KO[severity]}] {row['company']} · {row['title']}",
            f"- 구분: {category} · {row.get('kind', '')}",
            f"- 날짜: {date_text(row)}",
            "- 무엇이 바뀌었나:",
            *[f"  - {x}" for x in fact_lines(row, limit=5)],
        ]
        hits = (row.get("facts") or {}).get("hits") or []
        if hits:
            lines.append(f"- 감지한 말: {', '.join(hits)}")
        lines += [
            f"- 우리에게 미치는 영향: {impact_for(row, category, severity)}",
            f"- 권장 대응: {action_for(row, category, severity)}",
            f"- 출처: {' / '.join(u for _, u in row.get('links', []) if u)} (확인일: {check_date})",
            "",
        ]
    lines += ["## 3. 확인할 일"]
    lines += [f"- {a}" for a in actions_for_rows(ordered)] or ["- 이번 주 바로 할 일 없음(관찰만)"]
    if notes:
        lines += ["", "## 참고: 수집 상태", *[f"- {x}" for x in notes]]
    if errors:
        lines += ["", "## 수집 오류", *[f"- {x}" for x in errors]]
    return "\n".join(lines).strip() + "\n"


def build_slack_ci_message(rows: list[dict[str, Any]], date_from: str, date_to: str) -> str:
    ordered = aggregate_rows(rows)
    period = f"{date_from}~{date_to}"
    counts = {s: sum(1 for r in ordered if r["severity"] == s) for s in SEVERITY_RANK}
    lines = [
        f"📊 *세이브택스 경쟁사 주간 요약*  |  `{period}`",
        f"감지 {len(ordered)}건 · 🚨 높음 {counts['HIGH']} · ⚠️ 보통 {counts['MEDIUM']} · ℹ️ 낮음 {counts['LOW']}" + ("  _(항목별 상세는 스레드 👇)_" if ordered else ""),
    ]
    if not ordered:
        lines += ["", "이번 기간 감지된 변화가 없습니다."]
    for sev in ["HIGH", "MEDIUM", "LOW"]:
        group = [r for r in ordered if r["severity"] == sev]
        if not group:
            continue
        lines += ["", f"*{SEVERITY_ICON[sev]} {SEVERITY_KO[sev]}*"]
        for row in group:
            head = f"• *{slack_escape(row['company'])}* · {slack_escape(row['title'])}  `{row['category']}`"
            if sev == "LOW":
                note = textwrap.shorten(fact_lines(row, limit=1)[0], width=90, placeholder="...")
                lines.append(f"{head}\n   {slack_escape(date_text(row))} · {slack_escape(note)} · {slack_links(row)}")
                continue
            lines.append(head)
            lines.append(f"   📅 {slack_escape(date_text(row))}")
            for fact in fact_lines(row, limit=2)[:3]:
                lines.append(f"   ▸ {slack_escape(textwrap.shorten(fact, width=150, placeholder='...'))}")
            lines.append(f"   → {slack_escape(impact_for(row, row['category'], sev))}")
            lines.append(f"   ↳ {slack_links(row)}")
    quiet = [c for c in COMPETITORS if c not in {r["company"] for r in ordered}]
    if quiet:
        lines += ["", f"_변화 없음: {', '.join(quiet)}_"]
    actions = actions_for_rows(ordered)
    if actions:
        lines += ["", "*이번 주 확인할 일*"]
        lines += [f"• ✅ {slack_escape(a)}" for a in actions[:5]]
    lines += ["", f"🔎 *상세 리포트:* {slack_link(html_report_url(), '보기')}"]
    return "\n".join(lines)


def build_slack_thread_messages(rows: list[dict[str, Any]], notes: list[str], errors: list[str]) -> list[str]:
    """스레드 답글: 항목마다 사실 → 바뀐 조항 → 영향 → 대응 → 근거."""
    ordered = aggregate_rows(rows)
    out: list[str] = []
    for idx, row in enumerate(ordered, start=1):
        sev = row["severity"]
        hits = (row.get("facts") or {}).get("hits") or []
        lines = [
            f"{SEVERITY_ICON[sev]} *{idx}. {slack_escape(row['company'])} · {slack_escape(row['title'])}*",
            f"구분 `{row['category']}` · {slack_escape(row.get('kind', ''))} · {slack_escape(date_text(row))} · 출처 {slack_escape(row.get('source', ''))}",
            "",
            "*무엇이 바뀌었나*",
            *[f"• {slack_escape(x)}" for x in fact_lines(row, limit=5)],
        ]
        if row.get("source_type") == "notice":
            lines += ["", "*본문 앞부분*", f"> {slack_escape(textwrap.shorten(row.get('summary') or '', width=500, placeholder='...'))}"]
        if hits:
            lines.append(f"_감지한 말: {slack_escape(', '.join(hits))}_")
        lines += [
            "",
            f"*우리에게 미치는 영향* {slack_escape(impact_for(row, row['category'], sev))}",
            f"*권장 대응* {slack_escape(action_for(row, row['category'], sev))}",
            f"*근거* {slack_links(row)}",
        ]
        out.append("\n".join(lines))
    if notes or errors:
        status = ["🛠️ *수집 상태*"]
        status += [f"• {slack_escape(x)}" for x in notes]
        status += [f"• ❗ {slack_escape(x)}" for x in errors]
        out.append("\n".join(status))
    return out


def build_period_report(config: dict[str, Any], date_from: str, date_to: str) -> str:
    rows, notes, errors, _ = build_competitor_data(config, date_from, date_to)
    return build_detailed_ci_report(rows, notes, errors, date_from, date_to)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor SaveTax refund competitors and notify changes.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--html-report", type=Path, default=DEFAULT_HTML_REPORT)
    parser.add_argument("--slack-report", type=Path, default=DEFAULT_SLACK_REPORT)
    parser.add_argument("--slack-thread", type=Path, default=DEFAULT_SLACK_THREAD)
    parser.add_argument("--send-existing", action="store_true", help="Send existing report files without collecting again")
    parser.add_argument("--notify", action="store_true", help="Send Slack/email when changes are detected")
    parser.add_argument("--notify-on-errors", action="store_true", help="Send notifications for fetch errors too")
    parser.add_argument("--force-notify", action="store_true", help="Send notification even when no change")
    parser.add_argument("--no-save", action="store_true", help="Do not update state file")
    parser.add_argument("--period-from", help="Build a one-off date-filtered report from YYYY-MM-DD")
    parser.add_argument("--period-to", help="Build a one-off date-filtered report through YYYY-MM-DD, inclusive")
    args = parser.parse_args(argv)

    config = load_json(args.config, {})

    if args.send_existing:
        report = args.report.read_text(encoding="utf-8") if args.report.exists() else ""
        html_report = args.html_report.read_text(encoding="utf-8") if args.html_report.exists() else None
        slack_message = args.slack_report.read_text(encoding="utf-8") if args.slack_report.exists() else report
        thread = load_json(args.slack_thread, []) if args.slack_thread.exists() else []
        send_slack(slack_message, thread)
        send_email(report, html_report)
        return 0

    if args.period_from or args.period_to:
        if not (args.period_from and args.period_to):
            raise SystemExit("--period-from and --period-to must be used together")
        state = load_json(args.state, {"sources": {}})
        rows, notes, errors, new_state = build_competitor_data(config, args.period_from, args.period_to, state)
        report = build_detailed_ci_report(rows, notes, errors, args.period_from, args.period_to)
        html_report = build_html_ci_report(rows, notes, errors, args.period_from, args.period_to)
        slack_message = build_slack_ci_message(rows, args.period_from, args.period_to)
        slack_thread = build_slack_thread_messages(rows, notes, errors)
        args.report.write_text(report, encoding="utf-8")
        args.html_report.write_text(html_report, encoding="utf-8")
        args.slack_report.write_text(slack_message + "\n", encoding="utf-8")
        save_json(args.slack_thread, slack_thread)
        if not args.no_save:
            save_json(args.state, new_state)
        print(report)
        print("\n--- Slack message preview ---\n" + slack_message)
        print("\n--- Slack thread preview ---\n" + "\n\n-----\n".join(slack_thread))
        if args.notify:
            send_slack(slack_message, slack_thread)
            send_email(report, html_report)
        else:
            print("No notification sent")
        return 0

    previous_existed = args.state.exists()
    previous = load_json(args.state, {"sources": {}})

    fetched = fetch_all(config)
    changes, new_state, errors = detect_changes(fetched, previous, config)
    report = format_report(changes, errors, previous_existed)

    args.report.write_text(report, encoding="utf-8")
    print(report)

    if not args.no_save:
        save_json(args.state, new_state)

    if args.notify and (args.force_notify or should_notify(changes, errors, args.notify_on_errors)):
        send_slack(report)
        send_email(report)
    else:
        print("No notification sent")

    # Monitoring collection failures should be visible in the report, but should not
    # break the weekly GitHub Actions schedule or prevent state/report artifacts.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

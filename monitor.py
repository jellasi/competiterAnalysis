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


def send_slack(report: str) -> None:
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    channel_id = os.getenv("SLACK_CHANNEL_ID", "").strip()
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()

    text = report
    if len(text) > 3500:
        text = text[:3500] + "\n...보고서가 길어 일부 생략되었습니다. GitHub Actions artifact/last_report.md를 확인하세요."

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


def send_email(report: str) -> None:
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
        dt = parse_iso_datetime(article.get("updated_at") or article.get("created_at") or "")
        if in_period(dt, start, end_exclusive):
            rows.append({
                "company": company,
                "source": source["label"],
                "kind": "공지/약관/고객센터 업데이트",
                "date": dt.isoformat() if dt else "",
                "title": article.get("title") or "",
                "url": article.get("html_url") or url,
                "summary": summarize_body(article.get("body") or ""),
            })
    return rows


def collect_appstore_period(company: str, source: dict[str, Any], start: datetime, end_exclusive: datetime) -> list[dict[str, Any]]:
    item = fetch_appstore(company, source)
    if not item.ok:
        return []
    meta = item.metadata or {}
    dt = parse_iso_datetime(meta.get("currentVersionReleaseDate") or "")
    if not in_period(dt, start, end_exclusive):
        return []
    return [{
        "company": company,
        "source": source["label"],
        "kind": "App Store 앱 업데이트",
        "date": dt.isoformat() if dt else "",
        "title": f"{meta.get('trackName') or item.title} v{meta.get('version') or ''}".strip(),
        "url": meta.get("trackViewUrl") or item.url,
        "summary": summarize_body(meta.get("releaseNotes") or meta.get("description") or ""),
    }]


def collect_google_play_period(company: str, source: dict[str, Any], start: datetime, end_exclusive: datetime) -> list[dict[str, Any]]:
    item = fetch_webpage(company, source)
    if not item.ok:
        return []
    lines = item.normalized_text.splitlines()
    update_date = None
    release_notes = []
    stop_markers = {"flag 부적절한 앱으로 신고", "개발자", "google store", "모두 vat 포함된 가격입니다.", "대한민국 (한국어)", "앱 지원", "개발자 소개"}
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
    return [{
        "company": company,
        "source": source["label"],
        "kind": "Google Play 앱 업데이트",
        "date": update_date.isoformat() if update_date else "",
        "title": item.title or source["label"],
        "url": item.url,
        "summary": summarize_body("\n".join(release_notes) or item.text),
    }]


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


def row_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(k, "")) for k in ["kind", "title", "summary", "source"])


def classify_ci_row(row: dict[str, Any]) -> tuple[str, str]:
    text = row_text(row).lower()
    if "app store" in text or "google play" in text or "앱 업데이트" in text:
        category = "제품"
    elif any(k in text for k in ["프로모션", "이벤트", "캠페인"]):
        category = "프로모션"
    elif any(k in text for k in ["세무조사", "종합소득세", "간이과세", "세금", "환급 사례", "콘텐츠", "신고"]):
        category = "마케팅"
    elif any(k in text for k in ["약관", "개인정보", "처리방침", "환불", "수수료"]):
        category = "기타"
    else:
        category = "기타"

    # HIGH is reserved for direct pricing/revenue/customer-churn/core-position impact.
    if any(k in text for k in ["서비스 종료", "서비스 중단", "수수료 변경", "가격 변경", "환불 정책"]):
        severity = "HIGH"
    elif any(k in text for k in ["개인정보", "처리방침", "약관", "서비스 확대", "제3자"]) or ("전체" in text and "탭" in text):
        severity = "MEDIUM"
    elif "앱 업데이트" in text and any(k in text for k in ["사용성", "오류", "버그", "개선"]):
        severity = "LOW"
    elif category == "마케팅":
        severity = "LOW"
    else:
        severity = "LOW"
    return category, severity

def impact_for(row: dict[str, Any], category: str, severity: str) -> str:
    text = row_text(row)
    if "개인정보" in text or "처리방침" in text or "제3자" in text:
        return "개인정보 제공/활용 범위 변화는 환급 서비스 신뢰도와 동의 UX에 영향을 줄 수 있어 약관·동의 플로우 비교 확인이 필요합니다."
    if "전체" in text and "탭" in text:
        return "앱 내 서비스 탐색 구조를 넓히는 변화로, 환급 외 부가 서비스 노출·교차판매 UX 강화 가능성이 있습니다."
    if "사용성" in text or "오류" in text or "버그" in text:
        return "직접적인 포지션 변화는 제한적이나, 신청/조회 과정의 이탈률 개선 경쟁으로 이어질 수 있습니다."
    if category == "마케팅":
        return "세무 정보성 콘텐츠를 통한 SEO/신뢰 확보 활동으로 보이며, 즉각적 기능 변화보다는 상단 퍼널 유입 경쟁 측면에서 참고가 필요합니다."
    return "우리 서비스에 대한 직접 영향은 현재 데이터만으로 확인 필요합니다."


def action_for(row: dict[str, Any], category: str, severity: str) -> str:
    text = row_text(row)
    if "개인정보" in text or "처리방침" in text or "제3자" in text:
        return "삼쩜삼 개인정보 처리방침 개정 전후 조항을 비교하고, 세이브택스 환급의 개인정보 동의·제3자 제공 고지와 차이를 점검합니다."
    if "전체" in text and "탭" in text:
        return "비즈넵 앱의 신규 정보구조를 실제 앱 화면 기준으로 확인하고, 환급 외 부가 서비스 노출 방식과 온보딩 흐름을 캡처합니다."
    if "사용성" in text or "오류" in text or "버그" in text:
        return "덧셈/비즈넵의 최신 앱 버전을 설치해 환급 조회·신청 핵심 플로우의 단계 수와 이탈 지점을 비교합니다."
    if category == "마케팅":
        return "동일 키워드군의 검색 노출 현황을 확인하고, 세이브택스 콘텐츠/랜딩에서 보강할 주제를 선별합니다."
    return "변화의 실제 서비스 영향 여부를 추가 확인합니다."


def previous_change_text(row: dict[str, Any]) -> str:
    if row.get("kind", "").endswith("앱 업데이트") or "앱 업데이트" in row.get("kind", ""):
        return "해당 기간 내 앱 버전/릴리즈노트 업데이트로 확인. 직전 버전 대비 상세 화면 변화는 확인 필요."
    return "해당 기간 내 게시글 신규/수정으로 확인. 이전 리포트와의 신규/변경/지속 이슈 구분은 이전 리포트 데이터 확인 필요."


def evidence_line(row: dict[str, Any], check_date: str) -> str:
    return f"{row.get('url')} (확인일: {check_date})"


def build_competitor_data(config: dict[str, Any], date_from: str, date_to: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    start = parse_date_start(date_from)
    end_exclusive = parse_date_end_exclusive(date_to)
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[str] = []

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
                else:
                    skipped.append(f"{company} / {source['label']}: 날짜 필터 가능한 공개 메타데이터가 없어 정기 스냅샷 비교 대상")
                for row in collected:
                    category, severity = classify_ci_row(row)
                    row["category"] = category
                    row["severity"] = severity
                    rows.append(row)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{company} / {source['label']}: {e!r}")

    rows.sort(key=lambda r: (r.get("date", ""), r.get("company", ""), r.get("source", "")))
    return rows, skipped, errors


def merge_key(row: dict[str, Any]) -> tuple[str, str, str]:
    title = re.sub(r"\s+", " ", row.get("title", "")).strip().lower()
    return (row.get("company", ""), row.get("category", ""), title)


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = merge_key(row)
        if key not in seen:
            seen[key] = row
            continue
        # Merge source URLs if duplicated across App Store / Play Store etc.
        existing = seen[key]
        if row.get("url") and row["url"] not in existing.get("url", ""):
            existing["url"] = existing.get("url", "") + " / " + row["url"]
    return list(seen.values())


def aggregate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge repeated low-level content updates into one competitor-level item."""
    deduped = dedupe_rows(rows)
    marketing_groups: dict[str, list[dict[str, Any]]] = {}
    others: list[dict[str, Any]] = []
    for row in deduped:
        if row.get("category") == "마케팅" and row.get("company") == "비즈넵 환급":
            marketing_groups.setdefault(row["company"], []).append(row)
        else:
            others.append(row)

    for company, group in marketing_groups.items():
        if len(group) == 1:
            others.extend(group)
            continue
        group.sort(key=lambda r: r.get("date", ""))
        titles = [r.get("title", "") for r in group]
        urls = [r.get("url", "") for r in group if r.get("url")]
        others.append({
            "company": company,
            "source": "고객센터/공지/약관",
            "kind": "세무 정보성 콘텐츠 업데이트",
            "date": group[0].get("date", ""),
            "title": f"세무 정보성 콘텐츠 {len(group)}건 업데이트",
            "url": " / ".join(urls[:5]),
            "summary": "업데이트 제목: " + "; ".join(titles),
            "category": "마케팅",
            "severity": "LOW",
        })
    return others


def executive_summary(rows: list[dict[str, Any]], competitors: list[str]) -> list[str]:
    if not rows:
        return ["이번 기간 확인된 주요 변화는 없습니다."]
    severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    top = sorted(rows, key=lambda r: (severity_rank.get(r.get("severity", "LOW"), 9), r.get("date", "")))[:3]
    bullets = []
    for row in top:
        why = impact_for(row, row.get("category", "기타"), row.get("severity", "LOW"))
        bullets.append(f"{row['company']}에서 {row['title']} 변화가 확인되었습니다. 중요도는 {row['severity']}이며, 우리에게 중요한 이유는 {why}")
    active = sorted({r["company"] for r in rows})
    quiet = [c for c in competitors if c not in active]
    if quiet:
        bullets.append(f"{', '.join(quiet)}는 날짜 메타데이터 기준 주요 업데이트가 확인되지 않았습니다. 억지 인사이트 없이 지속 모니터링합니다.")
    return bullets[:3]


def build_detailed_ci_report(rows: list[dict[str, Any]], skipped: list[str], errors: list[str], date_from: str, date_to: str) -> str:
    period = f"{date_from} ~ {date_to}"
    check_date = datetime.now(timezone.utc).date().isoformat()
    competitors = ["삼쩜삼", "덧셈컴퍼니", "비즈넵 환급"]
    our_company = "세이브택스 환급"
    focus = "앱 서비스 업데이트, 주요 홈페이지 변경, 약관/공지 변경"
    url = report_url()
    rows = aggregate_rows(rows)

    lines: list[str] = [
        f"# {period} 경쟁사 동향 리포트",
        "",
        "## 리포트 정보",
        f"- 리포트 기간: {period}",
        f"- 작성 기준일: {check_date}",
        f"- 분석 대상 경쟁사: {', '.join(competitors)}",
        f"- 우리 회사/서비스: {our_company}",
        f"- 주요 관심 영역: {focus}",
        "- 이전 리포트: 확인 필요",
        f"- 상세 리포트 URL: {url}",
        "",
        "## 1. Executive Summary",
    ]
    for bullet in executive_summary(rows, competitors):
        lines.append(f"- {bullet}")
    lines += ["", "## 2. 주요 변화"]

    if not rows:
        lines += ["- 이번 기간 입력 데이터 기준 주요 변화가 확인되지 않았습니다.", ""]
    else:
        severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        for row in sorted(rows, key=lambda r: (severity_rank.get(r.get("severity", "LOW"), 9), r.get("company", ""), r.get("date", ""))):
            category = row.get("category", "기타")
            severity = row.get("severity", "LOW")
            lines += [
                f"### [{row['company']}] {row['title']}",
                f"- 구분: {category}",
                f"- 중요도: {severity}",
                f"- 확인된 사실: {row['kind']}가 확인되었습니다. {row.get('summary') or '세부 내용은 출처 확인 필요'}",
                f"- 이전 대비 변화: {previous_change_text(row)}",
                f"- 우리에게 미치는 영향: {impact_for(row, category, severity)}",
                f"- 권장 대응: {action_for(row, category, severity)}",
                f"- 출처: {evidence_line(row, check_date)}",
                f"- 확인일: {check_date}",
                "",
            ]

    lines += ["## 3. 경쟁사별 상세 분석"]
    for comp in competitors:
        comp_rows = [r for r in rows if r.get("company") == comp]
        lines += ["", f"### {comp}"]
        if not comp_rows:
            lines.append("- 주요 활동: 이번 기간 날짜 메타데이터 기준 신규/변경 활동 확인 없음.")
            lines.append("- 방향성: 확인 필요.")
            lines.append("- 반복 패턴: 확인 필요.")
            continue
        cats = sorted({r.get("category", "기타") for r in comp_rows})
        lines.append(f"- 주요 활동: {len(comp_rows)}건 확인 ({', '.join(cats)}).")
        if any("앱 업데이트" in r.get("kind", "") for r in comp_rows):
            lines.append("- 방향성: 앱 사용성 또는 정보구조 개선 활동이 관찰됨.")
        elif any(r.get("category") == "마케팅" for r in comp_rows):
            lines.append("- 방향성: 세무 정보성 콘텐츠를 통한 유입/신뢰 확보 활동이 관찰됨.")
        else:
            lines.append("- 방향성: 확인 필요.")
        pattern_titles = "; ".join(r["title"] for r in comp_rows[:3])
        lines.append(f"- 반복 패턴: {pattern_titles}")

    lines += ["", "## 4. 시사점"]
    if rows:
        lines += [
            "- 기회 요인: 경쟁사의 약관/앱 UX/콘텐츠 변화를 기준으로 세이브택스 환급의 신뢰 고지, 신청 UX, 정보성 콘텐츠 차별화 포인트를 점검할 수 있습니다.",
            "- 위협 요인: 앱 정보구조 개선과 세무 콘텐츠 확장은 환급 서비스 탐색성·상단 퍼널 경쟁을 강화할 수 있습니다.",
            "- 추가 확인이 필요한 사항: 실제 앱 화면 변화, 약관 개정 전후 조항, 홈페이지/약관 정적 페이지의 문구 변경 여부는 추가 수동 확인이 필요합니다.",
        ]
    else:
        lines += [
            "- 기회 요인: 확인 필요.",
            "- 위협 요인: 확인된 주요 변화 없음.",
            "- 추가 확인이 필요한 사항: 날짜 메타데이터가 없는 홈페이지/약관 페이지는 정기 스냅샷 비교로 보완 필요.",
        ]

    actions: list[tuple[str, str, str, str, str]] = []
    if any("개인정보" in row_text(r) or "처리방침" in row_text(r) for r in rows):
        actions.append(("삼쩜삼 개인정보 처리방침 개정 조항 비교", "동의/제3자 제공 고지 경쟁 수준 파악", "HIGH", "Product/Legal", "1주 이내"))
    if any("앱 업데이트" in r.get("kind", "") for r in rows):
        actions.append(("경쟁사 최신 앱 플로우 캡처", "환급 조회·신청 UX 차이 확인", "MEDIUM", "Product/Design", "1주 이내"))
    if any(r.get("category") == "마케팅" for r in rows):
        actions.append(("세무 정보성 콘텐츠 키워드 비교", "SEO/랜딩 콘텐츠 보강 주제 도출", "MEDIUM", "Marketing/Growth", "2주 이내"))
    if not actions:
        actions.append(("정기 모니터링 유지", "의미 있는 변화 발생 시 대응", "LOW", "Growth", "다음 정기 리포트"))

    lines += ["", "## 5. 권장 액션"]
    for idx, (item, purpose, priority, owner, due) in enumerate(actions, start=1):
        lines += [
            f"### {idx}. {item}",
            f"- 실행 항목: {item}",
            f"- 실행 목적: {purpose}",
            f"- 우선순위: {priority}",
            f"- 권장 담당 조직: {owner}",
            f"- 권장 완료 시점: {due}",
            "",
        ]

    if skipped:
        lines += ["## 참고: 날짜 필터 미지원 소스", ""]
        lines += [f"- {x}" for x in skipped]
        lines.append("")
    if errors:
        lines += ["## 수집 오류", ""]
        lines += [f"- {x}" for x in errors]
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_slack_ci_message(rows: list[dict[str, Any]], date_from: str, date_to: str) -> str:
    rows = aggregate_rows(rows)
    period = f"{date_from}~{date_to}"
    url = report_url()
    severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    top = sorted(rows, key=lambda r: (severity_rank.get(r.get("severity", "LOW"), 9), r.get("date", "")))[:3]
    lines = [f"📊 {period} 경쟁사 주간 요약"]
    if not top:
        lines.append("주요 변화: 입력 데이터 기준 확인된 주요 변화 없음")
    for row in top:
        icon = "🚨 " if row.get("severity") == "HIGH" else ""
        why = impact_for(row, row.get("category", "기타"), row.get("severity", "LOW"))
        why = textwrap.shorten(why, width=90, placeholder="...")
        change = textwrap.shorten(row.get("title", ""), width=80, placeholder="...")
        lines.append(f"- {icon}[{row.get('severity','LOW')}] {row['company']}: {change} → {why}")

    actions = []
    if any("개인정보" in row_text(r) or "처리방침" in row_text(r) for r in rows):
        actions.append("삼쩜삼 개인정보 처리방침 개정 전후 비교")
    if any("앱 업데이트" in r.get("kind", "") for r in rows):
        actions.append("경쟁사 최신 앱 플로우 캡처/비교")
    if any(r.get("category") == "마케팅" for r in rows):
        actions.append("세무 콘텐츠 키워드·랜딩 보강점 확인")
    if actions:
        lines.append("권장 액션: " + " / ".join(actions[:3]))
    lines.append(f"상세 리포트: {url}")
    msg = "\n".join(lines)
    if len(msg) > 1200:
        msg = msg[:1160].rstrip() + f"...\n상세 리포트: {url}"
    return msg


def build_period_report(config: dict[str, Any], date_from: str, date_to: str) -> str:
    rows, skipped, errors = build_competitor_data(config, date_from, date_to)
    return build_detailed_ci_report(rows, skipped, errors, date_from, date_to)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor SaveTax refund competitors and notify changes.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--notify", action="store_true", help="Send Slack/email when changes are detected")
    parser.add_argument("--notify-on-errors", action="store_true", help="Send notifications for fetch errors too")
    parser.add_argument("--force-notify", action="store_true", help="Send notification even when no change")
    parser.add_argument("--no-save", action="store_true", help="Do not update state file")
    parser.add_argument("--period-from", help="Build a one-off date-filtered report from YYYY-MM-DD")
    parser.add_argument("--period-to", help="Build a one-off date-filtered report through YYYY-MM-DD, inclusive")
    args = parser.parse_args(argv)

    config = load_json(args.config, {})

    if args.period_from or args.period_to:
        if not (args.period_from and args.period_to):
            raise SystemExit("--period-from and --period-to must be used together")
        rows, skipped, errors = build_competitor_data(config, args.period_from, args.period_to)
        report = build_detailed_ci_report(rows, skipped, errors, args.period_from, args.period_to)
        slack_message = build_slack_ci_message(rows, args.period_from, args.period_to)
        args.report.write_text(report, encoding="utf-8")
        (args.report.parent / "last_slack_message.txt").write_text(slack_message + "\n", encoding="utf-8")
        print(report)
        print("\n--- Slack message preview ---\n" + slack_message)
        if args.notify:
            send_slack(slack_message)
            send_email(report)
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

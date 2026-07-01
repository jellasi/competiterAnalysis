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
from datetime import datetime, timezone
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
    stop_markers = {"리뷰", "리뷰 모두 보기", "앱 정보", "데이터 보안", "평점 및 리뷰"}
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
    lines = [f"# 세이브택스 환급 경쟁사 변경 모니터링 리포트", "", f"- 실행 시각: {date_kst_hint}", f"- 감지 변경: {len(changes)}건", f"- 수집 오류: {len(errors)}건", ""]
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
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        print("SLACK_WEBHOOK_URL not set; skip Slack notification")
        return
    title = "세이브택스 경쟁사 변경 모니터링"
    text = report
    if len(text) > 3500:
        text = text[:3500] + "\n...보고서가 길어 일부 생략되었습니다. GitHub Actions artifact/last_report.md를 확인하세요."
    payload = json.dumps({"text": f"*{title}*\n```{text}```"}).encode("utf-8")
    req = Request(webhook, data=payload, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
    with urlopen(req, timeout=20) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Slack webhook failed: HTTP {resp.status}")
    print("Slack notification sent")


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
    msg["Subject"] = "[세이브택스] 경쟁사 변경 모니터링 알림"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor SaveTax refund competitors and notify changes.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--notify", action="store_true", help="Send Slack/email when changes are detected")
    parser.add_argument("--notify-on-errors", action="store_true", help="Send notifications for fetch errors too")
    parser.add_argument("--force-notify", action="store_true", help="Send notification even when no change")
    parser.add_argument("--no-save", action="store_true", help="Do not update state file")
    args = parser.parse_args(argv)

    config = load_json(args.config, {})
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

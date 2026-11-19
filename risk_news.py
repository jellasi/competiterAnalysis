#!/usr/bin/env python3
"""Daily public-news radar for tax-refund competitors and shared industry risks.

Search follows sr-agent's Naver/Serper approach when credentials are available.
Public Bing/Google News RSS feeds keep the radar useful without those credentials.
Only public article metadata is stored; allegations are labelled by their stage.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from monitor import ROOT, USER_AGENT, post_slack_bot, save_json

KST = timezone(timedelta(hours=9))
STATE_PATH = ROOT / "state" / "risk_news_state.json"
REPORT_PATH = ROOT / "last_risk_news.md"
SLACK_PATH = ROOT / "last_risk_slack_message.txt"
CHANNEL_ID = "C0BEG3NF24C"

SEARCHES = [
    '"삼쩜삼"', '"비즈넵"', '"덧셈컴퍼니"', '"덧셈" "환급"',
    '"토스인컴"', '"토스" "세금 환급"', '"세이브택스"', '"세이브 환급"',
    '"세무플랫폼"', '"환급 플랫폼"', '"세무대리 오인광고"',
    '"세무사법" "플랫폼"', '"경정청구" "플랫폼"',
    '"환급 수수료" "플랫폼"', '"세무플랫폼" "개인정보"',
]
ALIASES = {
    "삼쩜삼": ("삼쩜삼", "자비스앤빌런즈"),
    "비즈넵": ("비즈넵", "지엔터프라이즈"),
    "덧셈": ("덧셈컴퍼니", "덧셈"),
    "토스인컴": ("토스인컴", "토스 인컴", "토스 세금 환급", "토스 세금환급"),
    "세이브택스": ("세이브택스", "세이브 환급", "세이브환급", "히든머니"),
}
INDUSTRY_TERMS = ("세무플랫폼", "세무 플랫폼", "환급플랫폼", "환급 플랫폼", "세무대리", "경정청구", "세무사법")
EVENT_TERMS = (
    "고발", "고소", "수사", "송치", "기소", "압수수색", "제재", "시정명령", "과징금", "과태료",
    "소송", "판결", "위반", "규제", "세무사법", "표시광고", "오인광고", "허위광고", "과장광고",
    "개인정보 유출", "정보 유출", "서비스 중단", "서비스 장애", "환급 지연", "민원", "탈세",
    "개정", "시행", "입법예고", "행정예고", "고시", "가이드라인", "정책", "법안",
    "수수료", "환불", "출시", "개편", "인수", "합병", "투자", "제휴", "사업 종료",
)
STAGES = [
    ("판결", ("대법", "법원", "판결", "선고")),
    ("제재", ("시정명령", "과징금", "과태료", "영업정지", "제재")),
    ("기소·송치", ("기소", "송치")),
    ("고발·신고", ("고발", "고소", "공정위 신고", "경찰에 신고")),
    ("조사", ("조사 착수", "수사", "압수수색", "현장 조사")),
    ("입법·행정", ("법안", "입법예고", "행정예고", "고시", "개정", "시행")),
    ("소비자 보호", ("환급 지연", "소비자 보호", "이용자 보호", "소비자 피해", "민원")),
    ("사업 변화", ("출시", "개편", "인수", "합병", "투자", "제휴", "수수료", "환불", "종료")),
]
OFFICIAL_HOSTS = ("kacta.or.kr", "kacpta.or.kr", "nts.go.kr", "ftc.go.kr", "pipc.go.kr", "gov.kr", "assembly.go.kr")


@dataclass
class Article:
    title: str
    url: str
    source: str
    published: datetime
    summary: str = ""
    engine: str = ""
    company: str = ""
    stage: str = ""
    priority: str = ""
    evidence: list[tuple[str, str]] = field(default_factory=list)


def get_text(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> str:
    req = Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as response:
                return response.read(2_000_000).decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("news request failed")


def canonical_url(url: str) -> str:
    parts = urlparse(html.unescape(url))
    if parts.netloc.endswith("bing.com") and parts.path.endswith("apiclick.aspx"):
        target = parse_qs(parts.query).get("url", [""])[0]
        if target:
            return canonical_url(target)
    query = [(k, v) for k, values in parse_qs(parts.query).items() for v in values
             if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", urlencode(query), ""))


def published_at(value: str) -> datetime | None:
    relative = value.lower().strip() if value else ""
    if relative in ("yesterday", "어제"):
        return datetime.now(KST) - timedelta(days=1)
    if relative in ("today", "오늘"):
        return datetime.now(KST)
    match = re.match(r"(\d+)\s*(hour|hr|minute|min|day|week|시간|분|일|주)s?\s*(ago|전)?", relative)
    if match:
        number, unit = int(match.group(1)), match.group(2)
        scale = 60 if unit.startswith(("hour", "hr", "시간")) else 1
        if unit.startswith(("day", "일")):
            scale = 60 * 24
        elif unit.startswith(("week", "주")):
            scale = 60 * 24 * 7
        return datetime.now(KST) - timedelta(minutes=number * scale)
    try:
        return parsedate_to_datetime(value).astimezone(KST)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KST)
        except (TypeError, ValueError):
            return None


def parse_rss(xml_text: str, engine: str, start: datetime, end: datetime) -> list[Article]:
    rows: list[Article] = []
    root = ET.fromstring(xml_text)
    for item in root.findall("./channel/item"):
        dt = published_at(item.findtext("pubDate") or "")
        if not dt or not start <= dt <= end:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", item.findtext("title") or "")).strip()
        source = (item.findtext("source") or "").strip()
        if engine == "google" and source and title.endswith(" - " + source):
            title = title[:-(len(source) + 3)]
        url = canonical_url(item.findtext("link") or "")
        summary = html.unescape(re.sub(r"<[^>]+>", "", item.findtext("description") or "")).strip()
        if not title or not url:
            continue
        if not source:
            source = urlparse(url).netloc
        rows.append(Article(title, url, source, dt, summary[:600], engine))
    return rows


def fetch_rss(query: str, engine: str, start: datetime, end: datetime) -> list[Article]:
    if engine == "bing":
        url = "https://www.bing.com/news/search?" + urlencode({"q": query, "format": "rss", "mkt": "ko-KR"})
    else:
        days = max(2, (end.date() - start.date()).days + 2)
        url = "https://news.google.com/rss/search?" + urlencode({"q": f"{query} when:{days}d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    return parse_rss(get_text(url), engine, start, end)


def fetch_naver(query: str, start: datetime, end: datetime) -> list[Article]:
    client_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    client_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return []
    url = "https://naverapihub.apigw.ntruss.com/search/v1/news?" + urlencode({"query": query.replace('"', ''), "display": 100, "sort": "date"})
    data = json.loads(get_text(url, headers={"x-ncp-apigw-api-key-id": client_id, "x-ncp-apigw-api-key": client_secret}))
    rows = []
    for item in data.get("items", []):
        dt = published_at(item.get("pubDate", ""))
        if not dt or not start <= dt <= end:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", item.get("title", ""))).strip()
        summary = html.unescape(re.sub(r"<[^>]+>", "", item.get("description", ""))).strip()
        url = canonical_url(item.get("originallink") or item.get("link") or "")
        if title and url:
            rows.append(Article(title, url, urlparse(url).netloc, dt, summary[:600], "naver"))
    return rows


def fetch_serper(query: str, start: datetime, end: datetime) -> list[Article]:
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        return []
    payload = json.dumps({"q": query, "gl": "kr", "hl": "ko", "num": 100, "tbs": f"qdr:d{max(2, (end.date()-start.date()).days+2)}"}).encode()
    data = json.loads(get_text("https://google.serper.dev/search", data=payload,
                               headers={"X-API-KEY": key, "Content-Type": "application/json"}))
    rows = []
    for item in data.get("organic", []):
        dt = published_at(item.get("date", ""))
        if not dt or not start <= dt <= end:
            continue
        url = canonical_url(item.get("link", ""))
        if item.get("title") and url:
            rows.append(Article(item["title"], url, urlparse(url).netloc, dt, item.get("snippet", "")[:600], "serper"))
    return rows


def classify(article: Article) -> bool:
    text = f"{article.title} {article.summary}".lower()
    title = article.title.lower()
    title_companies = [name for name, aliases in ALIASES.items() if any(alias.lower() in title for alias in aliases)]
    companies = title_companies or [name for name, aliases in ALIASES.items() if any(alias.lower() in text for alias in aliases)]
    industry = any(term in text for term in INDUSTRY_TERMS)
    event = any(term.lower() in text for term in EVENT_TERMS)
    # A matching article must name a watched service, or discuss the refund/tax-agent market.
    if not event or not (companies or industry):
        return False
    # Generic tax returns and ordinary customer FAQ are outside this news radar.
    if not companies and not any(term in title for term in ("플랫폼", "세무대리", "경정청구")):
        return False
    # The SaveTax brand also sells bookkeeping; keep this radar tied to refunds or regulation.
    if "세이브택스" in companies and not any(term in text for term in ("환급", "경정청구", "세무사법", "개인정보", "고발", "제재", "규제", "소송")):
        return False
    article.company = ", ".join(title_companies) if title_companies else ("업계 공통" if any(term in title for term in INDUSTRY_TERMS) else ", ".join(companies))
    article.stage = next((label for label, terms in STAGES if any(term in text for term in terms)), "동향")
    serious = ("고발·신고", "기소·송치", "제재", "판결", "조사")
    article.priority = "높음" if article.stage in serious or ("세이브택스" in companies and article.stage == "입법·행정") else "보통"
    article.evidence = [(article.source, article.url)]
    return True


def title_key(article: Article) -> str:
    text = re.sub(r"[^0-9a-z가-힣]+", "", article.title.lower())
    for alias in (alias for aliases in ALIASES.values() for alias in aliases):
        text = text.replace(re.sub(r"[^0-9a-z가-힣]+", "", alias.lower()), "")
    return text


def issue_key(article: Article) -> str:
    text = f"{article.title} {article.summary}".lower()
    if article.stage == "사업 변화":
        if "월세지원센터" in text or ("앱" in text and "개편" in text):
            return "앱 개편·월세지원센터"
        for label, terms in (("수수료", ("수수료", "환불")), ("인수합병", ("인수", "합병")),
                             ("투자", ("투자",)), ("제휴", ("제휴",)), ("종료", ("종료",))):
            if any(term in text for term in terms):
                return label
    return ""


def group_articles(articles: list[Article]) -> list[Article]:
    grouped: list[Article] = []
    for article in sorted(articles, key=lambda a: (a.published, a.engine == "bing"), reverse=True):
        if not classify(article):
            continue
        match = next((old for old in grouped if old.company == article.company and old.stage == article.stage
                      and abs((old.published - article.published).total_seconds()) <= 2 * 86400
                      and (canonical_url(old.url) == canonical_url(article.url)
                           or (old.stage in ("고발·신고", "기소·송치", "제재", "판결", "조사")
                               and old.published.date() == article.published.date())
                           or (issue_key(old) and issue_key(old) == issue_key(article))
                           or difflib.SequenceMatcher(None, title_key(old), title_key(article)).ratio() >= .62)), None)
        if match:
            if (article.source, article.url) not in match.evidence:
                match.evidence.append((article.source, article.url))
            if match.url.startswith("https://news.google.com") and not article.url.startswith("https://news.google.com"):
                match.url, match.source, match.summary = article.url, article.source, article.summary
        else:
            grouped.append(article)
    return sorted(grouped, key=lambda a: (a.priority == "높음", a.published), reverse=True)


def event_id(article: Article) -> str:
    key = "" if article.stage in ("고발·신고", "기소·송치", "제재", "판결", "조사") else (issue_key(article) or title_key(article)[:90])
    signature = f"{article.company}|{article.stage}|{article.published.date()}|{key}"
    return hashlib.sha256(signature.encode()).hexdigest()[:20]


def already_seen(article: Article, seen: dict) -> bool:
    if event_id(article) in seen:
        return True
    for value in seen.values():
        if not isinstance(value, dict) or value.get("company") != article.company or value.get("stage") != article.stage:
            continue
        try:
            day_gap = abs((article.published.date() - datetime.fromisoformat(value["published"]).date()).days)
        except (KeyError, ValueError):
            continue
        if day_gap > 2:
            continue
        if article.stage in ("고발·신고", "기소·송치", "제재", "판결", "조사"):
            return True
        if issue_key(article) and issue_key(article) == value.get("issue_key"):
            return True
        if difflib.SequenceMatcher(None, title_key(article), value.get("title_key", "")).ratio() >= .5:
            return True
    return False


def format_outputs(items: list[Article], start: datetime, end: datetime, errors: list[str]) -> tuple[str, str, list[str]]:
    period = f"{start:%Y-%m-%d}~{end:%Y-%m-%d}"
    lines = [f"# 세무플랫폼 경쟁·규제 뉴스 | {period}", "", f"검색: {len(SEARCHES)}개 주제 / 기사 사건 {len(items)}건", ""]
    for article in items:
        lines += [f"## [{article.priority}] {article.company} · {article.stage} · {article.title}",
                  f"- 게시: {article.published:%Y-%m-%d %H:%M} KST / {article.source}",
                  f"- 근거: {article.url}",
                  f"- 내용: {article.summary[:320] or '제목 기준 후보 — 원문 확인 필요'}",
                  *(["- 단계 주의: 고발·신고·조사는 위반 확정이 아님."] if article.stage in ("고발·신고", "조사") else []),
                  *[f"- 함께 보도: {source} {url}" for source, url in article.evidence if url != article.url][:4], ""]
    if errors:
        lines += ["## 수집 오류", *[f"- {error}" for error in errors], ""]
    report = "\n".join(lines).strip() + "\n"
    headline = [f"📰 *세무플랫폼 경쟁·규제 뉴스* | {period}", f"새 사건 *{len(items)}건* (높음 {sum(x.priority == '높음' for x in items)}건)"]
    for article in items[:5]:
        headline.append(f"• *[{article.priority} · {article.stage}] {article.company}* {article.title[:100]}\n  <{article.url}|{article.source}> · {article.published:%m/%d}")
    if len(items) > 5:
        headline.append(f"• 그 밖에 {len(items)-5}건은 답글에 링크")
    headline.append("※ 고발·조사 보도는 혐의 제기 단계입니다. 사실관계와 회사 영향은 원문 확인 후 판단하세요.")
    replies = []
    for article in items[5:]:
        links = " · ".join(f"<{url}|{source}>" for source, url in article.evidence[:3])
        replies.append(f"*[{article.priority} · {article.stage}] {article.company}* {article.title}\n{links}")
    return report, "\n".join(headline), replies


def collect(start: datetime, end: datetime) -> tuple[list[Article], list[str], int]:
    rows: list[Article] = []
    errors: list[str] = []
    successes = 0
    for query in SEARCHES:
        for engine in ("bing", "google"):
            try:
                rows.extend(fetch_rss(query, engine, start, end))
                successes += 1
            except Exception as exc:
                errors.append(f"{engine} / {query}: {type(exc).__name__}")
            time.sleep(.35)
        for engine, fetcher in (("naver", fetch_naver), ("serper", fetch_serper)):
            if engine == "naver" and not (os.getenv("NAVER_CLIENT_ID") and os.getenv("NAVER_CLIENT_SECRET")):
                continue
            if engine == "serper" and not os.getenv("SERPER_API_KEY"):
                continue
            try:
                rows.extend(fetcher(query, start, end))
                successes += 1
                if engine == "naver":
                    time.sleep(1)  # Match sr-agent's Naver request spacing.
            except Exception as exc:
                errors.append(f"{engine} / {query}: {type(exc).__name__}")
    return group_articles(rows), errors, successes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3, help="Overlapping KST calendar days to search")
    parser.add_argument("--notify", action="store_true", help="Post new events to the competitor Slack channel")
    parser.add_argument("--all", action="store_true", help="Include previously seen events in the preview")
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    args = parser.parse_args()
    if not 1 <= args.days <= 60:
        parser.error("--days must be 1..60")
    if args.notify and not os.getenv("SLACK_BOT_TOKEN"):
        parser.error("SLACK_BOT_TOKEN is required for --notify")
    if args.notify and args.all:
        parser.error("--all is for preview only")
    end = datetime.now(KST)
    start = (end - timedelta(days=args.days)).replace(hour=0, minute=0, second=0, microsecond=0)
    found, errors, successes = collect(start, end)
    if successes == 0:
        print("No news source succeeded: " + ", ".join(errors), file=sys.stderr)
        return 1
    try:
        state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {"seen": {}}
    except (OSError, ValueError):
        state = {"seen": {}}
    seen = state.get("seen", {})
    new = [article for article in found if args.all or not already_seen(article, seen)]
    report, slack, replies = format_outputs(new, start, end, errors)
    REPORT_PATH.write_text(report, encoding="utf-8")
    SLACK_PATH.write_text(slack + "\n", encoding="utf-8")
    print(report)
    if args.notify and new:
        token = os.environ["SLACK_BOT_TOKEN"]
        parent = post_slack_bot(token, CHANNEL_ID, slack)["ts"]
        for reply in replies:
            post_slack_bot(token, CHANNEL_ID, reply[:3800], parent)
            time.sleep(1)
        print(f"Slack sent: https://teamunitblack.slack.com/archives/{CHANNEL_ID}/p{parent.replace('.', '')}")
    elif args.notify:
        print("No new events; no Slack post")
    else:
        print("Preview only; no Slack post")
    if args.notify:
        for article in found:
            seen[event_id(article)] = {"posted_at": end.isoformat(), "company": article.company,
                                       "stage": article.stage, "published": article.published.date().isoformat(),
                                       "title_key": title_key(article), "issue_key": issue_key(article)}
        # Bound state while retaining enough overlap for follow-up reports.
        cutoff = (end - timedelta(days=120)).isoformat()
        state["seen"] = {key: value for key, value in seen.items()
                         if (value if isinstance(value, str) else value.get("posted_at", "")) >= cutoff}
        state["updated_at"] = end.isoformat()
        save_json(args.state, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

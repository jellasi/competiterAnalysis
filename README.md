# 세이브택스 환급 경쟁사 변경 모니터

삼쩜삼, 덧셈컴퍼니, 비즈넵 환급의 앱/홈페이지/약관/공지 변경을 매주 1회 확인하고 Slack 채널과 이메일로 알림을 보내는 GitHub Actions 기반 모니터입니다.

## 감시 대상

설정 파일: `sources.json`

- 삼쩜삼
  - 홈페이지
  - 고객센터 공지사항/약관 개정 안내
  - 서비스 이용약관
  - Google Play
  - App Store
- 덧셈컴퍼니
  - 홈페이지
  - 이용규칙/약관
  - Google Play
  - App Store
- 비즈넵 환급
  - 비즈넵 환급 홈페이지
  - 비즈넵 홈페이지
  - 고객센터/공지/약관
  - Google Play
  - App Store

## 실행 주기

`.github/workflows/weekly-competitor-monitor.yml` 기준:

```yaml
cron: "0 23 * * 0"
```

매주 월요일 08:00 KST에 실행됩니다.

## 첫 실행 동작

첫 실행에는 비교 기준이 없으므로 `state/competitor_state.json` 기준 스냅샷만 저장하고, 기본적으로 변경 알림은 발생하지 않습니다.

테스트 알림을 보내고 싶으면 GitHub Actions에서 수동 실행할 때 `force_notify=true`를 선택하세요.

## GitHub Secrets 설정

기본 수신자:

```text
minseok.cho@unitblack.co.kr
jellasi@naver.com
```

`EMAIL_TO` secret을 별도로 설정하면 이 기본 수신자를 덮어씁니다.

GitHub 저장소의 Settings → Secrets and variables → Actions → Repository secrets에 아래 값을 추가하세요.

### Slack 알림

| Secret | 설명 |
|---|---|
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |

Slack Webhook 만드는 법:

1. Slack API에서 Incoming Webhooks 앱 생성 또는 기존 앱 사용
2. 알림 받을 채널 선택
3. Webhook URL을 복사
4. GitHub Secret `SLACK_WEBHOOK_URL`에 저장

### 이메일 알림

| Secret | 설명 | 예시 |
|---|---|---|
| `SMTP_HOST` | SMTP 서버 | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP 포트 | `587` |
| `SMTP_USERNAME` | SMTP 로그인 계정 | `yourname@gmail.com` |
| `SMTP_PASSWORD` | SMTP 비밀번호/앱 비밀번호 | Gmail은 앱 비밀번호 권장 |
| `SMTP_USE_SSL` | SSL 직접 연결 여부 | `false` for 587, `true` for 465 |
| `EMAIL_FROM` | 발신 이메일 | `yourname@gmail.com` |
| `EMAIL_TO` | 수신 이메일. 미설정 시 기본값 사용 | `minseok.cho@unitblack.co.kr,jellasi@naver.com` |

Gmail을 쓸 경우 일반 계정 비밀번호가 아니라 Google 계정의 **앱 비밀번호**를 쓰는 것을 권장합니다.

## 로컬 실행

```bash
python monitor.py
```

변경이 있을 때 Slack/Email도 보내려면 환경변수를 설정한 뒤:

```bash
python monitor.py --notify --notify-on-errors
```

변경이 없어도 테스트 알림을 보내려면:

```bash
python monitor.py --notify --force-notify
```

## 결과 파일

- `state/competitor_state.json`: 이전 실행 기준 스냅샷
- `last_report.md`: 최근 실행 리포트

GitHub Actions는 매 실행 후 두 파일을 artifact로 업로드하고, state/report 변경분을 repo에 커밋합니다.

## 주의사항

- Google Play는 동적/차단 정책이 있어 페이지 수집이 가끔 실패할 수 있습니다. 실패 내용은 리포트의 `수집 오류` 섹션에 표시됩니다.
- 웹페이지 전체 HTML이 아니라 텍스트를 정규화해서 비교합니다. 그래도 광고/배너/랜덤 문구 변경으로 알림이 발생할 수 있습니다.
- 너무 많은 알림이 오면 `sources.json`에서 덜 중요한 URL을 제거하거나 `keywords`를 조정하세요.

## 기간 리포트 수동 발송

GitHub Actions의 `Run workflow`에서 아래 입력값을 넣으면 특정 기간의 실제 수집 데이터 리포트를 즉시 이메일/Slack으로 발송합니다.

```text
force_notify=true
period_from=2026-06-23
period_to=2026-06-28
```

`period_from` / `period_to`를 비우면 기존 주간 변경 감시 모드로 실행됩니다.

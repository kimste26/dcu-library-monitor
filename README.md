# 대구가톨릭대학교 신학도서관 신착자료 자동 알림

이 패키지는 **로그인 없이 공개적으로 열리는 신착자료 결과 페이지**를 주기적으로 확인해서,
새로운 자료가 나타나면 **ntfy / 이메일 / 텔레그램**으로 알려줍니다.

핵심은 학교 사이트 안에서 따로 “알림 서비스”를 찾는 게 아니라,
**신착자료 검색 결과 페이지 자체를 모니터링**하는 방식입니다.

## 이 방식이 맞는 이유

- 대구가톨릭대학교 중앙도서관 공개 메뉴에 `자료검색 > 신착자료`가 있습니다.
- 중앙도서관 공개 메뉴에서 `신학도서관` 링크도 따로 보입니다.
- 학교 자체평가보고서에는 중앙도서관·신학도서관·의학도서관이 모두 같은 **튤립 3.0** 시스템을 사용한다고 나옵니다.
- 그래서 **신학도서관 자체 신착자료 페이지** 또는 **중앙도서관 신착자료에서 신학도서관으로 필터링한 결과 URL** 중 하나를 감시 대상으로 잡는 방식이 가장 실용적입니다.

## 가장 쉬운 추천 구성

1. 브라우저에서 신착자료 페이지를 엽니다.
2. `신학도서관`으로 한 번 필터링합니다.
3. 주소창의 **최종 URL**을 복사합니다.
4. 그 URL을 `TARGET_URL`에 넣습니다.
5. GitHub Actions로 6시간마다 실행되게 둡니다.
6. 새 책이 생기면 ntfy나 이메일로 알림을 받습니다.

이렇게 하면 **학교 로그인 자동화는 필요 없고**, 공개 페이지 감시만 하면 됩니다.

---

## 파일 구성

- `dcu_theolib_monitor.py` : 실제 모니터링 스크립트
- `.github/workflows/dcu-theolib-monitor.yml` : GitHub Actions 스케줄 실행
- `.env.example` : 로컬 실행용 예시 환경변수
- `state/seen_items.json` : 이미 본 항목 저장 파일
- `debug/` : 디버그 HTML/스크린샷/파싱 결과(기본은 실패 시에만 활용)

---

## 추천: GitHub Actions로 항상 켜두기

### 1) 새 GitHub 저장소 만들기
이 폴더 전체를 그대로 올립니다.

### 2) GitHub Secrets 설정
저장소의 **Settings → Secrets and variables → Actions** 에서 아래를 넣습니다.

필수:
- `TARGET_URL` : 브라우저에서 최종 확인한 신학도서관 신착자료 결과 URL

선택:
- `INCLUDE_TEXT` : 중앙도서관 전체 페이지를 감시할 때만 `신학도서관`
- `NTFY_TOPIC` : ntfy 토픽 이름
- `NTFY_SERVER` : 기본은 `https://ntfy.sh`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASS`
- `SMTP_FROM`
- `EMAIL_TO`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 3) Actions 활성화
워크플로를 수동 실행하거나, 스케줄을 기다리면 됩니다.

### 4) 첫 실행 주의
첫 실행은 **기준선(baseline)** 만 저장하고, 기존 책 전체를 한꺼번에 알리지 않습니다.
그 다음 실행부터 새로 생긴 항목만 보냅니다.

---

## ntfy로 푸시 알림 받기 (가장 간단)

1. 휴대폰에 ntfy 앱 설치
2. 임의의 길고 랜덤한 토픽 이름 하나 정하기
   - 예: `dcu-theolib-8f2b1c4d`
3. 앱에서 그 토픽을 구독
4. GitHub Secret `NTFY_TOPIC`에 같은 값을 저장

그러면 새 신착자료가 생길 때마다 폰으로 푸시가 옵니다.

---

## 이메일로 받기

SMTP 계정이 있으면 됩니다.

필요 값:
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASS`
- `SMTP_FROM`
- `EMAIL_TO`

보안상 이 값들은 모두 GitHub Secrets에 넣는 걸 권장합니다.

---

## 로컬에서 직접 돌리기

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp .env.example .env
# .env 값을 수정한 뒤
export $(grep -v '^#' .env | xargs)
python dcu_theolib_monitor.py
```

리눅스 서버라면 `cron`으로 6시간마다 돌릴 수 있습니다.

예시:
```cron
0 */6 * * * cd /path/to/dcu_theolib_monitor && /path/to/python dcu_theolib_monitor.py >> monitor.log 2>&1
```

---

## 사이트 구조가 바뀌면

기본적으로 이 스크립트는 결과 페이지 안의 `a[href*='/search/detail/']` 링크를 찾아서 새 항목을 판별합니다.

만약 학교 사이트 HTML 구조가 바뀌면:
- `DETAIL_LINK_SELECTOR` 를 수정하거나
- `TARGET_URL` 을 다시 잡아주면 됩니다.

로컬 실행 시 디버그 파일은 `debug/` 폴더에 남습니다. GitHub Actions에서는 실패 시 `dcu-theolib-debug` 아티팩트로 업로드되게 해 두었습니다.

---

## 현실적인 팁

가장 안정적인 방식은 아래 둘 중 하나입니다.

### 방법 A: 신학도서관 사이트 자체의 신착자료 URL
예상상 가장 깔끔합니다.

### 방법 B: 중앙도서관 신착자료에서 `신학도서관` 필터를 적용한 결과 URL
실제로 브라우저에서 잘 뜨는 URL이면 이 방식이 제일 확실합니다.

즉, **“필터링 과정을 자동화”하기보다, “필터가 이미 적용된 결과 URL”을 주기적으로 감시**하는 쪽이 훨씬 덜 깨집니다.

---

## 실패했을 때 점검 순서

1. 브라우저에서 `TARGET_URL` 이 로그인 없이 열리는지 확인
2. 해당 페이지에 실제로 신착자료 목록이 보이는지 확인
3. `debug/last_page.png` 에 목록이 찍혔는지 확인
4. 실패한 GitHub Actions 실행에서 `dcu-theolib-debug` 아티팩트를 내려받아 `last_page.png`, `last_page.html`, `last_items.json` 을 확인
5. 알림 채널(`NTFY_TOPIC` 또는 SMTP/Telegram) 값이 맞는지 확인

---

## 권장 세팅

- 가장 간단: `GitHub Actions + ntfy`
- 메일이 꼭 필요: `GitHub Actions + SMTP 이메일`
- 텔레그램 사용 중이면: `GitHub Actions + Telegram`


# 누보 포장지 발주관리 시스템 — 프로토타입 (v0)

`docs/포장지관리시스템_PRD.md`의 P0-1~P0-8을 구현한 웹앱 프로토타입입니다. 로컬은 Django + SQLite로 바로 구동되고, 외부 공유 테스트용으로 Render(웹서비스) + Supabase(PostgreSQL·파일저장소) 무료 배포가 준비되어 있습니다(아래 "외부 배포" 참고).

## 실행 방법 (Windows)

이 저장소를 만들 때 사용한 Python은 **표준 빌드 3.13**입니다. `py` 기본값이 free-threaded 빌드(3.13t)로 잡혀있으면 Pillow 등 설치가 실패하므로 반드시 `py -3.13`으로 가상환경을 만드세요.

```powershell
cd app
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

python manage.py migrate
python manage.py seed_demo_data
python manage.py runserver
```

브라우저에서 http://127.0.0.1:8000 접속.

## 계정 (비밀번호 공통: `1234`)

| 아이디 | 이름 | 역할 |
|---|---|---|
| haon | 이정인 매니저 | 요청자 (울산공장) |
| isis9 | 박현경 팀장 | 1차 검토·관리 창구 (브랜드기획팀) |
| shindeok_kim | 김신덕 본부장 | 박현경 팀장의 대체 담당자 |
| guychj | 최효진 차장 | 디자인 수정 |
| hjcho | 조현종 소장 | 최종 검수·반려 판단 (연구소) |
| nousbo | 관리자 | 전체 슈퍼 관리자, Django 관리자(`/admin/`) |

## 한 바퀴 돌려보는 시나리오

1. `haon`으로 로그인 → 대시보드 → "재발주 요청 등록" → 품목([품목코드] 품목명) 선택 후 등록 → 자동 채번된 요청번호(`RQ-YYYYMMDD-###`)가 부여됩니다.
2. `isis9`으로 로그인 → 대시보드에 대기건 노출 → 건 상세에서 "최종본 확인" 또는 "수정 필요" 처리.
   - "수정 필요"를 선택하면 `guychj`로 로그인해 AI/JPG 파일을 업로드 → 다시 `isis9`이 재확인.
3. `isis9`이 "최종본 확인"으로 넘기면 `hjcho`로 로그인해 최종검수 처리(승인/수정필요/반려). 반려는 사유 입력이 필수입니다.
4. 승인되면 다시 `isis9`으로 로그인해 "최종파일 전달·완료 처리".
5. `haon`이 완료된 건에서 최종 승인파일을 다운로드.

부재중/대체 담당자: `isis9`으로 로그인 후 상단 "부재중 설정"을 누르면, 이후 `shindeok_kim`의 대시보드에도 동일한 대기건이 노출됩니다.

일괄 업로드/다운로드: 상단 메뉴의 "일괄 업로드/다운로드"에서 확인.

알림: 상단 "알림함"이 인앱 알림입니다. 계정 이메일은 `{아이디}@nousbo.com` 형식입니다. `.env`의 `EMAIL_HOST` 등을 채우면 실제 메일로 발송되고(아래 "이메일 실제 발송 설정"), 비워두면 콘솔/`logs/notifications.log`에만 기록되는 모의(mock) 발송입니다. 카카오톡/문자는 발신 API 계약이 아직 없어 항상 로그로만 남습니다.

### 이메일 실제 발송 설정

`.env` 파일에 SMTP 정보를 채우면 즉시 실제 발송으로 전환됩니다(코드 수정 불필요):

```
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=noreply@nousbo.com
EMAIL_HOST_PASSWORD=앱비밀번호또는SMTP비밀번호
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=noreply@nousbo.com
```

`nousbo.com` 도메인의 메일함(각 아이디@nousbo.com)을 실제로 받아볼 수 있는 메일 호스팅(Google Workspace, 네이버웍스 등)의 SMTP 릴레이 정보가 필요합니다. 이 정보를 알려주시면 바로 연결해드릴 수 있습니다.

## 테스트

```powershell
python manage.py test
```

권한 분리(반려는 연구소만), 반려 사유 필수, 중복 요청 감지, 3개월 예외, 대체 담당자 라우팅, 파일 잠금/버전 승계 등 핵심 비즈니스 로직을 자동 테스트로 검증합니다.

## 품목코드 / 요청번호

- 품목은 이름이 아니라 **품목코드**(`code`, 고유값)로 식별됩니다. 품목명은 향후 변경될 수 있어 검색·매칭·연동의 안정적인 기준이 되지 못하기 때문입니다. 일괄 업로드 시 엑셀 매핑표에 품목코드를 지정하면, 이미 존재하는 코드는 품목명이 바뀌었더라도 같은 품목으로 인식해 최신 정보로 갱신(upsert)됩니다. 매핑표 없이 파일명만으로 업로드하는 경우에는 코드를 알 수 없으므로 기존 품목명 매칭만 지원하며 신규 품목은 생성되지 않습니다.
- 모든 재발주 건에는 등록 시점에 `RQ-YYYYMMDD-###` 형식의 **요청번호**가 자동 채번되어 대시보드·상세페이지·알림에 일관되게 표시됩니다.

## 알려진 한계 (프로토타입 범위)

- 일괄 업로드 시 품목과 매칭되지 않은 파일은 등록되지 않고 목록으로만 안내됩니다 — 수동 재매칭 화면은 아직 없고, 엑셀 매핑표를 정정해 재업로드하거나 `/admin/`에서 직접 등록해야 합니다.
- 매핑표로 신규 품목을 생성할 때 유형(라벨/PP포대)·제품군(비료/작물보호제) 정보가 없으면 기본값(라벨/비료)으로 생성됩니다 — 필요 시 `/admin/`에서 보정하세요.
- 카카오톡/문자는 실제 발송되지 않고 로그로만 남습니다(외부 API 계약 필요, PRD Open Questions 참고). 이메일은 SMTP 정보를 설정하면 실제 발송됩니다.
- 인증은 Django 기본 로그인만 사용(사내 SSO 연동 없음).

## 외부 배포 — Render + Supabase (무료, 다같이 테스트용)

계정 생성은 각 서비스에 본인이 직접 가입해야 합니다(제3자가 대신 만들 수 없음). 코드는 이미 배포 준비가 되어 있어 아래 값만 채우면 됩니다.

### 0. GitHub에 올리기

Render는 GitHub 저장소를 연결해 배포합니다. 아직 git 저장소가 아니라면:

```powershell
cd "포장지관리서비스 구현"
git init
git add .
git commit -m "Initial commit"
```

그 다음 GitHub에서 새 저장소를 만들고 안내되는 `git remote add` / `git push` 명령으로 올리세요.

### 1. Supabase 프로젝트 만들기 (DB + 파일저장소, 무료)

1. [supabase.com](https://supabase.com) 가입 → **New Project** 생성 (리전은 가까운 곳 선택, DB 비밀번호는 따로 기록).
2. **Project Settings → Database → Connection string(URI)** 복사 → 이 값이 `DATABASE_URL`.
3. **Storage** 탭 → 새 Bucket 생성(예: `packaging-files`).
4. **Project Settings → Storage → S3 Connection** 에서 발급되는 값을 기록: Access Key ID / Secret Access Key / Endpoint URL / Region.

### 2. Render 웹서비스 만들기 (무료)

1. [render.com](https://render.com) 가입 → GitHub 계정 연동.
2. **New + → Blueprint** → 방금 올린 GitHub 저장소 선택 → 저장소 루트의 `render.yaml`을 자동으로 인식합니다.
   - Blueprint 대신 수동으로 만들 경우: New + → Web Service → 저장소 선택 → **Root Directory: `app`**, Build Command/Start Command는 `render.yaml` 내용 그대로 입력.
3. 생성 과정에서 아래 환경변수 입력 화면이 뜹니다 (`sync: false`로 표시된 항목):
   - `DATABASE_URL`: 위 Supabase Connection string
   - `AWS_STORAGE_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_S3_ENDPOINT_URL`: 위 Supabase Storage S3 정보 (업로드 파일 보관용 — 비워두면 로컬에 저장되어 재배포 시 파일이 사라집니다)
   - `EMAIL_HOST` 등: 실제 이메일 발송을 원하면 SMTP 정보 (비워두면 로그로만 남는 모의 발송)
4. **Deploy** 클릭 → 빌드 로그에서 `migrate`/`seed_demo_data`가 자동 실행되는 것을 확인 → 발급된 `https://*.onrender.com` 주소로 접속.

`DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`(Render 도메인), `CSRF_TRUSTED_ORIGINS`는 코드에서 자동 처리되어 별도 입력이 필요 없습니다.

### 참고

- Render 무료 웹서비스는 15분 이상 요청이 없으면 슬립 상태가 되고, 다음 접속 시 재기동에 30초~1분 정도 걸릴 수 있습니다(이후엔 정상 속도).
- 위 값들을 알려주시면 실제로 붙여서 배포를 완료해드릴 수도 있습니다 — 다만 계정 로그인이 필요한 단계(가입, 결제정보 없는 무료 플랜 선택 등)는 직접 진행해주셔야 합니다.

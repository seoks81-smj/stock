# 🚀 무료 클라우드 배포 가이드

이 가이드를 따라하면 **30분 안에** 명재님만의 눌림목 스크리너 웹사이트가 만들어집니다.
파이썬 설치도, 명령어도 필요 없어요. 마우스 클릭만 하면 돼요.

---

## 📋 준비물

1. **GitHub 계정** (없으면 만드세요, 무료)
2. **Render 계정** (없으면 만드세요, 무료)
3. **이메일 주소**

끝. 카드 등록도, 결제도 없어요.

---

## 1단계: GitHub에 코드 올리기

### 1-1. GitHub 가입
- https://github.com 접속 → "Sign up" 클릭
- 이메일·비밀번호·사용자명 입력해서 가입

### 1-2. 새 저장소(Repository) 만들기
- 로그인 후 우측 상단 **"+"** 버튼 → **"New repository"**
- Repository name: `pullback-scanner` (원하는 이름 OK)
- **Public** 선택 (무료 플랜은 Public만 가능)
- "Create repository" 클릭

### 1-3. 파일 업로드
저장소 페이지에서:
- **"uploading an existing file"** 링크 클릭
- 또는 **"Add file"** → **"Upload files"**
- **이 폴더의 모든 파일을 드래그 앤 드롭**:
  - `app.py`
  - `requirements.txt`
  - `Procfile`
  - `runtime.txt`
  - `render.yaml`
  - `templates/` 폴더 전체 (`index.html` 포함)
- 하단 **"Commit changes"** 클릭

⚠️ **중요**: `templates` 폴더가 그대로 보존되어야 해요. 폴더 구조가 깨지면 안 돼요.

---

## 2단계: Render에 배포하기

### 2-1. Render 가입
- https://render.com 접속
- 우측 상단 **"Get Started"** 클릭
- **"Sign in with GitHub"** 선택 (제일 편함)
- GitHub 인증 → Render 권한 허용

### 2-2. 새 웹 서비스 만들기
- 대시보드에서 **"New +"** 버튼 → **"Web Service"** 선택
- **"Build and deploy from a Git repository"** → "Next"
- 방금 만든 `pullback-scanner` 저장소 옆 **"Connect"** 클릭

### 2-3. 설정 입력
다음 항목을 입력하세요:

| 항목 | 값 |
|---|---|
| **Name** | `pullback-scanner` (또는 원하는 이름) |
| **Region** | Singapore (한국에서 가장 빠름) |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn app:app --timeout 120` |
| **Instance Type** | **Free** ⭐ |

### 2-4. 배포 시작
- 페이지 하단 **"Create Web Service"** 클릭
- 빌드 시작! (5~10분 정도 걸려요)
- 로그가 실시간으로 보임. 마지막에 `Your service is live 🎉` 나오면 성공!

### 2-5. URL 확인
- 페이지 상단에 `https://pullback-scanner-xxxx.onrender.com` 같은 URL이 보임
- 클릭하면 명재님 사이트 열림! 🎉

---

## 3단계: 핸드폰 홈 화면에 추가 (선택)

### iPhone (Safari)
1. 사이트 열기 → 하단 공유 버튼 (□↑)
2. **"홈 화면에 추가"** 선택
3. 이름 입력 후 **"추가"**
4. 홈에서 앱처럼 사용!

### Android (Chrome)
1. 사이트 열기 → 우측 상단 메뉴 (⋮)
2. **"홈 화면에 추가"** 선택
3. **"추가"** 클릭

---

## 🎯 사용 방법

1. 핸드폰/PC에서 사이트 열기
2. 종목코드 6자리 입력 (예: `039240`)
3. **"분석하기"** 버튼 클릭
4. 점수, 차트, 매매 가이드까지 자동 표시!

**관심종목 일괄 분석**:
- "관심종목 일괄" 탭 클릭
- 종목코드 여러 개 (한 줄에 하나씩) 입력
- 일괄 분석 → 점수 높은 순 정렬됨

---

## ⚠️ Free 플랜의 한계

Render 무료 플랜은:
- **15분 동안 사용 안 하면 서버 잠듦**
- 다시 접속하면 약 30초~1분 깨어나는 시간 필요
- 한 달에 750시간 무료 (충분함)

→ 첫 접속 시 좀 느려도 정상이에요. 깨어나면 그 다음부터는 빨라요.

---

## 🆘 문제 해결

### 빌드 실패 (Build failed)
- Render 대시보드 → 해당 서비스 → "Logs" 탭에서 에러 확인
- 보통 `requirements.txt` 누락이 원인. GitHub에 모든 파일 올라갔는지 확인.

### 사이트 열렸는데 에러 페이지
- "Logs" 탭 확인
- `templates/index.html`이 `templates` 폴더 안에 제대로 들어있는지 확인

### 종목 분석 실패
- pykrx는 주말에 불안정할 수 있어요. 평일 저녁에 시도하세요.
- 너무 작은 종목(거래일 수 부족)은 분석 안 될 수 있어요.

### "Application failed to respond"
- 첫 접속 시 서버가 깨어나는 중. 30초~1분 후 새로고침.

---

## 💡 코드 수정하고 싶을 때

GitHub에서 직접 파일 편집 가능:
1. 저장소 → 수정할 파일 클릭
2. 연필 아이콘 (Edit) 클릭
3. 수정 후 "Commit changes"
4. Render가 자동으로 재배포 (3~5분)

분석 기준 바꾸고 싶으면 `app.py` 상단의 `CONFIG`를 수정하세요.

---

## 🎉 완료!

이제 명재님은 **자기만의 주식 분석 웹앱 운영자**가 되셨어요. 핸드폰에서 종목코드만 치면 즉시 눌림목 분석이 나오는 진짜 자동화 도구입니다.

URL은 명재님만 알고 있으니까 사적인 도구로 쓰셔도 되고, 동료들과 공유하셔도 돼요.

문제 생기면 언제든 물어보세요! 🚀

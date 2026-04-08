# Global Tax Free News Report

Streamlit 기반 뉴스 크롤링 웹앱입니다. 네이버/Google 뉴스 수집, 감정 분석, 키워드 추출, 이슈 묶음 표시, CSV/Excel 다운로드까지 한 번에 처리할 수 있습니다.

## 주요 기능

- 키워드 또는 카테고리 기준 뉴스 검색
- 네이버 뉴스 + Google 뉴스 수집
- 실제 발행일 기준 정렬/표시
- 제목 유사도 + 발행시간 근접성 기반 이슈 묶음
- 감정 분석 및 키워드 추출
- CSV / Excel 다운로드
- 외부 서버 배포용 설정 포함

## 프로젝트 구조

```text
news_crawler_app/
├─ app.py
├─ crawler.py
├─ analyzer.py
├─ run_streamlit.py
├─ requirements.txt
├─ Dockerfile
├─ Procfile
├─ runtime.txt
├─ .streamlit/
│  └─ config.toml
└─ assets/
   └─ global-tax-free-logo.png
```

## 로컬 실행

```bash
pip install -r requirements.txt
streamlit run app.py
```

브라우저에서 `http://localhost:8501`로 접속하면 됩니다.

## 외부 웹사이트 배포

이 저장소는 바로 배포할 수 있도록 준비되어 있습니다. 실제 공개 URL 생성은 GitHub, Streamlit Community Cloud, Render, Railway, VPS 같은 배포 계정 또는 서버가 있어야 합니다.

### 방법 1. Streamlit Community Cloud

1. 이 프로젝트를 GitHub 저장소에 올립니다.
2. Streamlit Community Cloud에서 저장소를 연결합니다.
3. 메인 파일은 `app.py`를 선택합니다.
4. 배포가 끝나면 공개 URL이 생성됩니다.

`requirements.txt`와 `runtime.txt`가 포함되어 있어 바로 배포할 수 있는 형태입니다.

### 방법 2. Docker로 서버 배포

```bash
docker build -t gtf-news-report .
docker run -p 8501:8501 gtf-news-report
```

서버에서 실행한 뒤 `http://서버주소:8501`로 접속하면 됩니다.

### 방법 3. Procfile 지원 호스팅(Render, Railway 등)

이 저장소에는 `Procfile`과 `run_streamlit.py`가 포함되어 있어, `PORT` 환경변수를 쓰는 호스팅 서비스에서도 바로 실행할 수 있습니다.

## 로컬 수정 후 배포 반영 방법

외부 웹사이트로 운영하더라도 작업 방식은 지금과 거의 같습니다.

1. 로컬에서 코드 수정
2. 로컬에서 확인
3. 배포 서버 또는 저장소에 반영
4. 배포 서비스에서 재시작 또는 자동 재배포

즉, 앞으로도 로컬에서 수정한 뒤 배포본에 반영하는 흐름으로 운영하면 됩니다.

## 환경 변수

- `PORT`: 배포 서버가 지정하는 포트. `run_streamlit.py`가 자동 반영합니다.
- `STREAMLIT_SERVER_ADDRESS`: 기본값은 `0.0.0.0`
- `NEWS_CRAWLER_USE_SYSTEM_PROXY=1`: 시스템 프록시를 강제로 사용할 때만 설정

## 참고

- 뉴스 사이트 구조가 바뀌면 크롤링 로직을 조정해야 할 수 있습니다.
- 일부 서버 환경에서는 외부 뉴스 사이트 접속 정책에 따라 크롤링 결과가 달라질 수 있습니다.
- 로고 원본 이미지를 그대로 쓰려면 `assets/global-tax-free-logo.png` 경로에 파일을 두면 됩니다.

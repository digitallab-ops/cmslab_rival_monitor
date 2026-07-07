# K-Beauty Intel — 경쟁사 인텔리전스 시스템

K-뷰티 주요 경쟁 브랜드의 글로벌 동향을 자동으로 수집·분류·시각화하는 모니터링 시스템입니다.

**대시보드:** https://cmslab-rival-monitor.onrender.com

---

## 무엇을 수집하나

구글 뉴스, 네이버, 장업신문, Reddit 등 국내외 6개 소스에서 경쟁 브랜드 관련 기사를 매일 자동 수집합니다.

**모니터링 브랜드 (9개)**  
Anua · Beauty of Joseon · Dr.Jart+ · Skin1004 · Mediheal · Torriden · Dalba · By Wishtrend · Cos de Baha

**커버 국가 (16개)**  
US · JP · KR · GB · DE · FR · PL · SG · TH · ID · MY · VN · PH · AU · CA · CN

수집된 기사는 AI(OpenAI gpt-4o-mini)가 브랜드·시장·활동유형·중요도로 자동 분류하고 한국어로 번역해 저장합니다. 영어·일본어 기사도 한국어 제목과 요약으로 제공합니다.

---

## 어떻게 보여주나

- **Brand Radar** — 최근 4주 기사량 기반 모멘텀 순위. 급부상 브랜드를 한눈에 파악
- **브랜드 × 국가 히트맵** — 어느 브랜드가 어느 시장에서 활동 중인지 매트릭스로 표시
- **HIGH 기사 필터** — M&A, 신시장 진출, 대형 파트너십 등 중요도 높은 기사만 따로 확인
- **전략 인사이트** — 브랜드별 최근 활동 AI 요약
- **Slack 알림** — HIGH 기사 발생 즉시 + 매주 화요일 주간 브리핑 자동 발송

---

## 기술 기반

| 항목 | 내용 |
|------|------|
| 서버 | Render (FastAPI) |
| DB | Supabase PostgreSQL |
| AI 분류 | OpenAI gpt-4o-mini |
| 스케줄 | 매일 18:00 KST 자동 수집 |
| 누적 기사 | 1,300건+ |

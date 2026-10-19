---
name: adaptive-literature-review
description: 적응형 선행연구 조사·종합분석 에이전트(v1.3). 검색 모드 International(OpenAlex)/KCI/Integrated(국내외 통합, KCI는 인증키 실측 후 활성화)를 지원한다. 연구주제 또는 연구계획서 1개를 받아 연구분야별 권위 있는 학술지·학회를 우선 출처로 정해 먼저 탐색(Stage A)하고, 출처 제한 없는 인용망(Stage B)·독립검색(Stage C)으로 넓혀 이론/방법론/유사 실증 Seed 3편과 핵심문헌(Core) 최대 20편을 구성한다. Core 논문마다 연구 적합성 중요도 점수·전체/역할별 순위와 근거를 매기고, 공개 원문 확보, 인용근거 원문 대조, 논문별 분석카드, 장·절별 문헌활용계획, 종합보고서를 out/에 만들고, 연구계획서 v2·분석 중 질문·심사의견이 오면 부족 영역만 증분 확장한다. '선행연구 조사', '문헌조사', '문헌검토', 'literature review', '핵심논문 찾아줘', 'Seed 논문', '주요 학술지 위주로', '인용망 확장', '선행연구 보고서', '연구계획서에 인용할 논문', '추가 문헌', '연구계획 v2 반영', 'KCI 논문', '국내 논문', '국내외 통합 검색', '논문 중요도', '먼저 읽을 논문', '우선순위' 요청 시 사용한다. 정식 체계적 문헌고찰(Systematic Review) 수행이나 메타분석 계산 용도로는 사용하지 않는다.
---

# adaptive-literature-review — 적응형 선행연구 조사·종합분석 (v1.3)

역할 분담: **Skill(이 문서)** = 절차·판정 원칙 / **`lra.py`** = API·중복제거·다운로드·DB·인용 대조·렌더링·검증 / **에이전트(LLM)** = 연구맥락 해석·우선 출처 제안·후보 판단·논문 읽기·비교·서술 초안 / **사용자** = 검색 모드·우선 출처 수정, Seed·Core·중요도 순위 승인(Pin), 연구공백·인용의 최종 판단.

## 0. 절대 규칙 (모든 단계에 우선)

1. **허구 금지.** 논문·DOI·저자·인용문·쪽수·수치·**출처 ID(S…)** 는 `lra.py`가 외부 DB나 원문에서 가져온 것만 쓴다. 기억에 있는 논문도 `add-paper --doi`로, 학술지·학회는 `source find`→`source add`로 실재가 확인돼야 등록된다. 확인되지 않으면 `확인 필요`.
2. **수준 구분.** 전문을 읽지 않은 논문은 `초록 기반`이다. 초록 기반 논문에서 직접 인용·쪽수·표 수치를 만들지 않는다. 인용 검증상태는 도구가 원문 대조로 정하며(숫자·부호는 그대로 비교, 4어절 이상, 생략은 `…`·`[...]`로 표시, 여러 쪽에 나오면 쪽 기재 필수), `VERIFIED_FULLTEXT`가 아닌 인용문은 보고서에 쓰지 않는다.
3. **`raw/`는 읽기 전용.** 수정·이동·덮어쓰기 금지. 새 버전은 새 파일명(`plan_v2.pdf`)으로.
4. **쓰기 위치:** `state/`(DB·작업 JSON·notes), `library/`(도구만 씀), `out/`. 그 외 파일 생성 금지.
5. **외부 전송 최소화.** 외부 API에는 검색식·DOI·식별자만 보낸다. 연구계획서 원문, 미발표 데이터, 고유한 비공개 용어는 검색식에 넣지 않는다. 필요하면 사용자 승인을 먼저 받는다.
6. **합법적 원문만.** `fetch`는 OA로 표시된 PDF만 받는다. 랜딩 페이지 스크래핑, 로그인, 유료벽·봇 차단 우회, 브라우저 위장 금지. 실패는 요청목록(06)으로. `library/` 원문을 `out/`이나 외부로 복사·배포하지 않는다.
7. **억지 충원 금지.** Core 20편은 목표치다. 관련성 낮은 논문으로 채우지 말고 부족 수량과 원인을 보고한다. 인용수·발행연도·**학술지 권위**만으로 선정·품질 판정하지 않는다.
8. **우선 출처는 검색 순서일 뿐이다.** 우선 학술출처는 Stage A의 탐색 순서를 정하는 가변 목록이며 배타적 포함조건이 아니다. Stage B·C에서는 출처 필터를 걸지 않고, **'우선 출처 밖'을 제외 사유로 쓰지 않는다**(도구가 거부함). OpenAlex `is_core`(CWTS Core 목록 플래그 — Web of Science Core Collection 아님)·`listed_in`은 출처 목록 등재 정보일 뿐 논문 품질 보증이 아니다.
9. **삭제 금지.** 제외·교체는 `deselect`(상태 변경)로만. 이력은 DB에 남는다. Core 제외·교체는 사용자 승인 후.
10. **승인 기록은 사용자 발화가 있을 때만.** `approve`·`source approve`는 사용자가 채팅에서 승인한 것에만 실행한다. 사용자가 "알아서 진행"을 요청한 데모에서는 `--provisional`로 **임시선정**임을 표시하고 보고서에도 밝힌다.
11. **연구공백은 가설.** "그런 연구는 없다"가 아니라 "본 검토 범위에서 확인되지 않음"으로 쓴다. Systematic Review를 수행했다고 표현하지 않는다.
12. **중요도 순위는 이 연구계획 버전의 상대적 활용 우선순위다.** 학술지·논문의 절대 서열이나 품질 확정값으로 표현하지 않는다. 인용수·학술지 지표·KCI 등재 상태는 점수 근거로 쓰지 않고 보조 표시만 한다. 중요도(Importance)·품질 검토(Quality)·근거 검증(Verification)은 서로 다른 필드다. 근거 없는 기준에 점수를 채우지 않는다(→ `평가보류`). 사용자 확인 없이 승인된 순위를 바꾸지 않는다.
13. **KCI는 실측 전 사용 금지.** KCI 응답 필드·검색 조건은 `kci probe`로 실제 확인되기 전에는 지원된다고 가정하지 않는다(도구가 `state/kci_probe.json`을 보고 차단하지만, 이 파일은 에이전트가 쓸 수 있는 위치이므로 **절차로 지킨다** — 직접 만들거나 고치지 않는다). KCI **학술지 인용지수를 논문 인용수로 쓰지 않으며**, KCI·해외 DB 인용수를 비교·합산하지 않는다. KCI 모드에서는 사용자 동의 없이 해외 문헌을 Core에 섞지 않는다(`select`·`seed set`이 `--user-consented-mix "동의 내용"` 없이는 거부하고, 동의 문구는 DB에 저장되며 V17이 확인한다). 통합 모드의 국내외 비중은 고정하지 않는다(예: 10:10 금지). 번역 제목만 비슷한 논문은 자동 병합하지 않는다(`REVIEW_DUPLICATE` → 사람 확인). DOI 없는 레코드는 같은 언어의 원제목과 첫 저자가 모두 같을 때만 자동으로 합친다. 중복 판정은 쌍 단위로 저장되어 재수집해도 지워지지 않는다.
14. **사람이 내릴 결정은 사람에게 묻는다.** `approve`·`rank approve`·`rank pin`·`mode set`·`kci probe`·`dedup --link/--unlink`는 프로젝트 `.claude/settings.json`의 권한 설정으로 실행 전에 사용자 확인 창이 뜬다. `approve`는 `--by`가 필수이고, `--by user`면 사용자가 한 말을 `--evidence "..."`로 그대로 남긴다. 사용자가 승인하지 않았으면 `--by agent`로 기록한다(임시선정 Seed 표시는 사용자 승인으로만 풀린다).

## 1. 준비

작업 폴더 = 프로젝트 루트(`raw/ state/ library/ out/`이 있는 곳). 아래에서
`LRA` = `python .claude/skills/adaptive-literature-review/scripts/lra.py` (프로젝트 루트에서 실행).

- 의존성: Python 3.10+, `requests`, `pymupdf`(설치돼 있지 않으면 사용자에게 설치 승인을 받는다).
- **OpenAlex 예산:** 키가 없으면 같은 IP가 하루 $0.10(목록 조회 약 100회)을 공유한다. 단건 조회(`/works/W…`, `/sources/S…`)는 무료. 20편 1회 실행에 목록 조회 약 25~45회가 든다. 키가 있으면 `state/config.json`에 `{"openalex_api_key": "..."}` 또는 환경변수 `OPENALEX_API_KEY`. **에이전트가 사용자 이메일로 키를 대신 신청하지 않는다** — 필요하면 https://openalex.org/settings/api 안내.
- 선택: `LRA_MAILTO`(Crossref polite pool), `S2_API_KEY`. 사용자가 명시적으로 제공한 경우에만 설정한다.
- 응답은 `state/cache/`에 캐시된다(캐시 적중은 호출·비용 0으로 기록). 최신화가 필요하면 `$LRA --no-cache …`. 단, `integrity`(정정·철회)는 철회 공지가 사후에 나올 수 있어 하루 지난 캐시는 다시 조회하며, 마지막 점검이 30일을 넘으면 V14가 WARN이다.
- 매 실행 결과의 `openalex_budget_remaining_usd`를 보고 0.01 미만이면 멈추고 사용자에게 알린다.
- v1.1·v1.2에서 만든 기존 프로젝트: 첫 실행 시 DB가 자동 이전된다. v1.1 프로젝트는 `$LRA refresh`(무료)로 기존 문헌의 출처 ID를 채운다.
- **KCI(2차 기능):** 한국연구재단 KCI Open API 인증키를 사용자가 발급받아 환경변수 `KCI_API_KEY` 또는 `state/config.json`의 `kci_api_key`에 넣는다(에이전트가 대신 신청하지 않음). 넣은 뒤 `$LRA kci probe --title "<짧은 한글 제목어>"`로 실측 → `state/kci_probe.json`에 실제 응답 필드·제목 필수 여부·참고문헌 API 형태가 기록되고, `verified: true`일 때만 KCI 검색이 열린다.

## 2. 절차 A — 최초 문헌조사 (우선 출처 → Seed 3편 → Core 20편)

### A1. 입력·상태 확인 (F01·F13)
```bash
$LRA status                         # 기존 프로젝트면 이어서 진행
$LRA init --title "<연구주제>"       # 최초 1회
$LRA register-raw                   # raw/ 해시 고정 + 텍스트 추출(state/text/raw/)
```
- 입력이 채팅의 주제 한 줄이면 `raw/`에 넣지 않아도 된다(그대로 Profile에 기록).
- `처리 제한`(스캔 PDF, 미지원 형식)이 나오면 사용자에게 텍스트/PDF 변환본을 요청한다.
- 사용자가 추천 논문(U3)을 줬으면: `$LRA add-paper --doi <DOI> --note "사용자 추천"`.
- 사용자가 선호·제외 학술지·학회(U2)를 말했으면 그대로 기록해 둔다(A2b에서 반영).
- **검색 모드(U2):** 사용자가 국내 논문·KCI·국내외 통합을 원하면 `$LRA mode set KCI|INTEGRATED --reason "<사용자 요청>" [--domestic-share "<연구목적에 따른 비중 메모>"]`. 말이 없으면 기본 `INTERNATIONAL`. KCI가 아직 실측 전이면 그 사실을 알리고(01b에 기록) OpenAlex로 진행할지, 키 발급 후 진행할지 묻는다.
- 사용자가 중요도 가중치·꼭 넣을 문헌·우선순위(U7)를 말했으면 기록해 둔다(A8b에서 반영).

### A2. Research Profile·검색전략 (F01·F04)
1. 연구계획서 추출본(`state/text/raw/...`)을 **전부** 읽고 `references/formats.md` §1 형식으로 `state/work/profile_v1.json`을 작성한다. **`research_fields`에 이론 분야 / 방법론 분야 / 실증 적용 분야를 나눠** 적는다(융합연구면 여러 개).
2. 입력에 없는 이론·방법·변수·배제조건은 확정하지 않는다 → `"미정"` + `undetermined`.
3. 역할별 검색식: theory 2~3개, methodology 2~3개, empirical 2~3개, independent 2개 이상(우선 출처·Seed 인용망과 무관한 분야·용어). 국문 주제면 영문 검색식을 기본으로 하고, 국내 문헌이 중요하면 한국어 검색식도 추가(OpenAlex의 국문 커버리지 한계를 04에 기록).
   KCI/INTEGRATED 모드면 `search_strategy_ko`에 **한글 검색어·동의어·제도/정책 용어·띄어쓰기 변형**(예: "연구개발 보조금", "R&D 보조금", "연구 개발 지원")을 따로 만든다. KCI는 명세상 제목(title) 검색이 기본이므로 짧은 제목어 위주로 쪼갠다.
4. 등록: `$LRA profile set --file state/work/profile_v1.json --source raw/<계획서>`

### A2b. 분야별 우선 학술출처 선정 (F01·F02·F04) — Stage A 준비
1. `research_fields`의 분야마다 후보를 찾는다(목록 조회 1회씩):
   ```bash
   $LRA source find --query "research policy" [--type journal|conference]
   ```
   결과의 Source ID·유형·`is_core`·`listed_in`·DOAJ·발행 건수를 본다. 2yr citedness는 보조지표로만.
2. **선정 기준(순서대로):** ① 분야 적합성 — Aims & Scope·학회 주제·실제 수록 연구가 연구질문과 맞는가(필요하면 `search --sources S… --limit 5`로 실제 수록 논문을 확인) ② 학술적 공신력 — 색인·목록 등재, 학술 공동체 인정, 심사 체계(참고 근거) ③ 분야 내 영향력(보조) ④ 출판 형태 — AI·컴퓨터과학은 주요 국제학회 프로시딩(`--type conference`)을 포함, 사회과학은 학술지 중심. Methodology는 해당 방법론 원전이 실린 다른 분야 출처도 검토.
3. 기본 **5~10곳 내외**(초기 탐색량 관리용 권고, 융합연구면 조정 가능 — 조정 사유를 `state/notes/01a.md`에). 분야를 하나의 고정 순위표로 섞지 않는다. 예시 목록(과학기술정책 *Research Policy* 등)을 주제와 무관하게 재사용하지 않는다.
   ```bash
   $LRA source add S9731383 --field "과학기술정책" --role theory --priority 1 \
        --scope-fit "R&D 정책 효과 연구 다수 수록" --reason "이론·실증 핵심 학술지"
   ```
   `source add`는 단건 조회로 ID 실재를 확인한 뒤에만 저장한다(없는 ID는 거부).
4. 사용자 선호 출처는 `--origin user`로 추가, 사용자 제외 요청은 `$LRA source exclude S… --reason "사용자 요청: …" --origin user` → 이후 **모든 Stage 검색에서 자동 제외**된다. 제외한 출처는 사용자가 되살리라고 했을 때만 `source add S… --user-restored "사용자 요청 내용"`으로 다시 넣을 수 있다.
5. **KCI/INTEGRATED 모드의 국내 학술지:** 국내 세부분야별 KCI 학술지를 정해 등록한다. KCI 검색 결과에 같은 학술지명이 실제로 나와야 등록된다(임의 생성 금지). 등재 구분은 KCI 포털에서 확인한 값만 쓰고, 모르면 비워 둔다(`확인 필요`로 표시됨).
   ```bash
   $LRA source add-kci --name "기술혁신연구" --verify-title "혁신" --field "기술경영" --role empirical \
        --kci-index-status "KCI 등재(2026-10 확인)" --reason "국내 R&D 정책 실증 다수"
   ```
   국내 학술지 권위가 개별 논문 품질을 보증하지 않는다.
6. `$LRA render 01 01a` → **사용자 승인 지점 ⓪**: Profile 요약·`미정` 항목·검색식·우선 출처표를 보여주고 추가·수정·제외를 받는다. 승인되면 `$LRA source approve <S-ID…>`. (빠른 진행을 원하면 생략 가능하되 `PROPOSED` 상태로 남기고 보고서에 명시.)

### A3. Stage A — 우선 출처 기반 사전 후보 탐색 (F02)
```bash
$LRA search --query "<검색식>" --purpose theory --sources preferred --limit 25 [--filter "publication_year:>2009"]
```
- 역할별 검색식을 `--sources preferred`(또는 역할에 맞는 S-ID 일부)로 먼저 실행한다. 이론 원전은 오래됐을 수 있으므로 theory 검색에는 연도 필터를 걸지 않는다.
- **결과가 적거나 특정 역할 문헌이 부족하면** 출처를 보완(`source add`)하거나 제한을 완화한다. 완화 검색은 Stage C로 실행하고 사유를 남긴다:
  `$LRA search --query "…" --purpose methodology --stage C --expansion-reason "Stage A 방법론 후보 3건뿐 — 원전이 타 분야 학술지에 있음"`
- 사용자가 준 기간·언어 조건만 `--filter`로 적용한다(예: `publication_year:2010-2025`, `language:en|ko`).
- 검토: `$LRA candidates --top 60 --out state/work/candidates.md` (★ = 우선 출처) → 읽고 판단.
- 결정은 모두 기록: `$LRA screen P-012 P-019 --decision exclude --category relevance --reason "연구질문과 무관(의료 데이터 공유)"`. 제외(`screen --decision exclude`, `deselect --status REJECTED`)에는 `--category relevance|role|quality|duplicate|other`가 필수이고, '우선 출처 밖'·'주요 학술지 미수록' 같은 출처 기반 사유는 도구가 거부한다.
- **KCI 검색(KCI/INTEGRATED, 실측 통과 후):**
  ```bash
  $LRA search --db kci --query "연구개발 보조금" --purpose empirical --sources preferred   # Stage A: 등록한 KCI 학술지별 조회
  $LRA search --db kci --query "R&D 지원 효과" --purpose independent                       # Stage C: 학술지 제한 없음
  ```
  `--kci-field keyword|abstract`는 실측에서 제목 없는 검색이 지원된 경우에만 쓸 수 있다. 결과에 `review_duplicate`가 있으면 A6의 중복 검토 대상이다.
  KCI에는 `--filter`의 연도 조건(`publication_year:2010-2020`, `>2009`, `<2021`, `2015`)만 적용되며, 다른 조건이 섞이면 도구가 거부한다(조용히 버리지 않음).

### A4. Seed 3편 선정 (F02·F06·F11)
- 역할별 1편: Theory(핵심 이론의 원전 또는 대표 정립 연구) / Methodology(본 연구가 쓰거나 비교할 방법의 대표 적용·방법론 논문) / Empirical(대상·맥락이 가장 가까운 실증연구).
- 기준 순서: 연구질문 관련성 → 역할 대표성 → 연구설계의 적절성 → (보조) 인용망 중심성. 인용수·출처 권위 단독 기준 금지. Stage A 후보가 부적합하면 Stage C 후보에서 고를 수 있다(사유 기록).
- 판본 중복 먼저 확인: `$LRA dedup` → 같은 연구면 `$LRA dedup --link <출판본> <프리프린트> --note "..."`, 다른 연구면 `$LRA dedup --distinct A B --note "..."`. 잘못 연결했으면 `$LRA dedup --unlink <판본>`. 연결되지 않은 판본 후보가 Core·Extended에 함께 있으면 V04가 FAIL이다.
```bash
$LRA seed set --role theory --paper P-004 --reason "..." --alternatives P-011,P-020 [--provisional] [--user-consented-mix "..."]
```
- 적합한 후보가 없으면 지정하지 않는다 → 02에 `없음/추가 탐색 필요`.
- `$LRA render 02` 후 **사용자 승인 지점 ①**: Seed 3편과 대안을 보여주고 승인·교체를 받는다. 승인되면 `$LRA approve P-004 P-009 P-015 --by user --evidence "<사용자가 한 승인 발화 그대로>"`. 교체는 `seed reject` + 새 `seed set`.

### A5. Stage B — 출처 제한 없는 인용망·의미 확장 (F03) — **필수**
각 Seed(및 중요한 후보)마다:
```bash
$LRA expand --paper P-004 --direction backward --limit 40            # 참고문헌
$LRA expand --paper P-004 --direction forward --limit 40 --query "<핵심어>"   # 후속 인용(관련성 필터)
$LRA expand --paper P-004 --direction related --limit 20             # 의미 유사
```
- `expand`는 항상 Stage B이며 출처 필터를 걸 수 없다(도구가 거부). 분야 밖 방법론 원전·고전·학회논문·공개 연구문헌도 개별 적합성으로 검토한다.
- 확장 결과가 지나치게 넓으면 `--query`로 좁히고, 부족하면 새 Core 후보에서 1회 더 expand.
- backward 결과가 `--limit`보다 많으면 인용수가 아니라 발행연도 내림차순으로 자르고, 자른 사실을 04 검색 기록(기타 필터)에 남긴다. 많이 잘렸으면 `--query`로 관련성 필터를 먼저 건다.

### A6. Stage C — 독립·광역 검증 (F03·F04) — **Core 수량이 차도 최소 1회 필수**
```bash
$LRA search --query "<independent 검색식>" --purpose independent --limit 30      # 출처 제한 없음 = Stage C
```
- 우선 출처 목록·Seed 인용망과 무관한 검색식·인접 학문·방법론 용어로 실행한다(Seed 선정 이후에 최소 1회).
- 비교: `$LRA coverage` → Stage별 발견 수, 우선 출처 밖 문헌, Stage A에서 못 찾은 문헌, C에서만 발견된 문헌, 호출량을 본다.
  C에서만 나온 적합 문헌이 많으면 우선 출처·검색식이 놓친 영역이 있다는 신호 → 출처 보완 또는 추가 C 검색.
- 기록: `$LRA coverage --record --missed "P-031,P-040" --decision "C에서 정책평가 방법론 2편 추가 발견 → 확장 후 종료"`
- **국내외 중복 정리(INTEGRATED):** DOI → KCI 제어번호 → OpenAlex ID → 국/영문 제목·연도·저자 순으로 도구가 자동 병합한다(`BOTH`). 번역 제목만 비슷한 경우는 병합하지 않고 `REVIEW_DUPLICATE`로 남긴다.
  `$LRA dedup --review` → 저자·연도·학술지·초록을 비교해 같은 연구면 `$LRA dedup --link <대표> <다른 판> --note "국문판/영문판 동일 연구"`, 다르면 `$LRA dedup --distinct A B --note "..."`. 확실하지 않으면 사용자에게 묻는다.
- KCI 모드에서는 국제 이론·방법론 원전이 빠질 위험을, 통합 모드에서는 한국어 논문이 영어 검색에서 누락될 위험을 `state/notes/01b.md`에 기록한다. KCI 모드에서 해외 문헌이 꼭 필요해 보이면 **누락 가능성을 알리고**, 사용자가 동의할 때만 `select … --user-consented-mix "<동의 내용>"`(또는 Extended로 분리).

### A7~A8. 선별·Core 확정 (F03·F04·F06·F14·F11)
1. `$LRA candidates --top 80 --undecided --out state/work/candidates2.md` 를 읽고 제목·초록으로 선별 → `screen` 기록. 우선 목록 외 논문도 **같은 기준**으로 심사.
2. 기본 배분 **이론 6 / 방법론 6 / 유사 사례 8**을 참고해(연구 유형에 따라 조정, 조정 사유 기록) Core 제안:
   `$LRA select P-021 P-030 --roles theory --reason "..."` (복수 역할 논문은 `--roles theory,empirical`, 1편으로 계산). **우선 출처 밖 문헌은 `--outside-reason "..."`에 이 출처에서 고른 이유를 따로 쓴다**(03 CSV `우선출처밖_선정이유`로 나가며, 일반 `--reason`과 별도로 V05가 확인).
3. 점검: `$LRA dedup` · `$LRA bibcheck` · `$LRA integrity`
   - `MISMATCH`/`DOI_NOT_FOUND` → `show`로 확인 후 교체 또는 `확인 필요` 표기.
   - `RETRACTED` → Core에서 제외 제안(`deselect --status REPLACEMENT_NEEDED`), 사용자에게 알림. `CORRECTED`·`EXPRESSION_OF_CONCERN` → 카드와 보고서에 표시.
4. `$LRA coverage --record --decision "..."` → `$LRA render 01a 03 04` → `state/notes/04.md`에 누락 가능 영역(우선 출처 편향, 언어·분야·방법론·최신성·인용망 편향)을 쓰고 다시 `render 04`.
5. **중요도 점수·순위 제안(A8b, V16):** Core(그리고 Extended) 각 논문을 5개 기준으로 **0~5점** 잠정 평가하고 기준마다 근거 문장을 단다(형식: `references/formats.md` §6).
   | 기준 | 기본 가중치 | 판단 근거 |
   |---|---:|---|
   | 연구질문 직접 관련성 | 35% | 핵심 개념·가설·대상·변수와의 직접 연결 |
   | 연구 내 역할·기여도 | 25% | 이론·방법·실증의 핵심 근거·원전·대표 사례 역할 |
   | 연구설계·근거의 적절성 | 20% | 연구유형별 방법·자료·측정의 적절성(F06 품질 검토 연계, 학술지 권위 아님) |
   | 정보의 차별성·보완성 | 10% | 다른 Core가 주지 못하는 관점·데이터·방법 |
   | 연구시점 적합성 | 10% | 최신 지식 반영 필요성 + 고전 원전의 적합성(오래됐다고 감점하지 않음) |
   ```bash
   $LRA rank score --file state/work/rank_v1.json      # 점수 = Σ(가중치×점수)×20, Core 전체·역할별 순위 자동 계산
   $LRA render 03 03a
   ```
   - 초록만 본 상태에서 판단할 수 없는 기준(특히 근거 적절성)은 **빈칸**으로 두어 `평가보류`로 남긴다. 추정 점수를 채우지 않는다.
   - 등급 `A 핵심 활용 / B 적극 참고 / C 보완 참고`는 고정 점수구간이 아니라 점수 분포·역할·근거를 보고 붙이고, 의미를 보고서에 밝힌다.
   - 동점은 도구가 공개 규칙(관련성 → 역할 → 근거 → 승인)으로 가르고, 그래도 같으면 공동 순위(`3=`)로 둔다. 임의로 서열을 만들지 않는다.
   - 사용자가 가중치를 바꾸면 `$LRA rank weights --file w.json --reason "<사용자 요청>" --by user`(새 가중치 버전으로 전체 재계산, 이전 기록 보존).
6. **사용자 승인 지점 ②**: Core 목록(역할·연도·출처·Stage·선정사유·부족 수량), **Stage A/B/C별 선정 수, 우선 출처 밖 중요 문헌**, **중요도 순위표(03a)** 를 보여주고 승인받는다 → `approve`(문헌) · `rank approve --by user`(순위). 사용자가 특정 문헌의 순위를 지정하면 `$LRA rank pin P-007 --rank 1 --reason "<사용자 사유>" --by user`(AI 제안 순위와 별도로 기록). 20편 미만이면 미충족 수량과 원인을 함께 보고.

### A9~A10. 원문 확보 (F05)
```bash
$LRA fetch --s2                     # Core+Extended 대상, OA PDF만
$LRA render 05 06
```
- 실패 사유(HTTP 403 = 출판사 봇 차단 등)는 06 요청목록에 자동 정리된다. 사용자에게 06을 안내: 정당한 권한으로 받은 PDF를 `raw/fulltext/`에 `P-007.pdf` 형태로 넣어 달라고 요청.
- 사용자가 넣으면: `$LRA register-raw` → `$LRA ingest-fulltext raw/fulltext` → `$LRA evidence reverify` → 해당 논문 카드·근거를 전문 기반으로 갱신.

### A11~A12. 논문별 분석·인용근거 (F06·F07)
- 전문 있는 논문: `state/text/P-xxx/p001.txt …`를 읽는다(초록·서론·방법·결과·한계 위주, 표는 원본 PDF를 Read 도구 `pages`로 확인 가능). 전문 없으면 `$LRA show P-xxx`의 초록만.
- 논문 수가 많으면 **Agent 도구로 4~5편씩 병렬 위임**할 수 있다. 위임 프롬프트에 이 절의 규칙과 `references/formats.md` §2·§3을 그대로 포함하고, 결과는 JSON만 반환받는다. 등록은 메인이 한다.
- 근거: 논문당 2~6개. 직접 인용은 ≤40단어, 원문 그대로 복사(철자·기호 포함). → `$LRA evidence add --file state/work/ev_batch1.json`
  - `CONFLICT` → 쪽수를 고쳐 같은 `evidence_id`로 재등록. `UNVERIFIED` → 인용문을 원문에서 다시 복사하거나 인용문 없이 재서술만 남긴다. **추정으로 고치지 않는다.**
- 카드: §3 형식 → `$LRA card --file state/work/cards_batch1.json`. 연구 유형별 품질 검토(RCT/준실험/관찰/질적/이론·논평/리뷰)는 설계·데이터·측정·추론 한계를 사실과 해석으로 구분해 쓰고 점수는 매기지 않는다. 학술지 권위는 품질 근거가 아니다. (카드의 Selection 행에 출처·Source ID·Stage·우선 출처 밖 여부가 자동 표시된다.)
- **원문 확인 후 중요도 재평가:** 전문을 읽어 판단이 달라졌거나 `평가보류` 기준을 채울 수 있으면 해당 논문만 다시 `rank score`(항목에 `"change_reason": "전문 확인: 표본 설계 확인"`). 이전 점수·순위는 이력으로 남는다(03a 부록 F-2).
- `$LRA render 07 08 03a`

### A13. 비교·연구공백 (F08)
- `state/notes/09_body.md`: 개념·이론 / 방법·데이터·측정 / 결과 방향·조건별 **비교표 + 해설**. 공통점·차이·상충과 그 원인 후보. 모든 비교 주장에 `[P-xxx; E-xxx-yy]`.
- 연구공백 후보 → `references/formats.md` §5 → `$LRA gap --file ...`
- `$LRA render 09 12`

### A14. 연구계획 활용계획 (F09)
- Profile의 `plan_sections`·가설·변수별로 `활용 논문–근거–목적–서술 방식–한계`를 §4 형식으로 → `$LRA mapping --file ...` (10에는 각 문헌의 중요도 순위·등급이 자동 표시되고, 원문이 없는 상위 문헌은 `원문 요청 우선`으로 표시된다 — 06 요청목록에서 상위 문헌부터 안내)
- 장·절이 없는 주제 입력이면 표준 장(서론/이론적 배경/선행연구/가설/방법/변수·지표/논의/결론)을 쓴다.
- `state/notes/10.md`에 장·절별 작성 안내를 쓰고 `$LRA render 10`.

### A15. 종합보고서 (F10)
- `references/report_template.md` 목차로 `state/notes/11_body.md` 작성(Ⅰ에 검색 모드·우선 출처 선정 근거·Stage A/B/C 프로토콜, Ⅱ에 **`{{RANKING_TABLE}}`**(Core 중요도 순위표가 자동 삽입됨)·Stage별 발견·선정 규모·우선 출처 밖 중요 문헌·국내외 구성) → `$LRA render` (전체 재생성, 부록 E=01a·부록 F=03a 자동 연결) → 선택: `$LRA export-bib`.

### A16. 최종 검증·사용자 검토 (F04·F07·F11·F14)
```bash
$LRA verify        # → out/16_검증결과.md
```
- `verify`는 FAIL이 있으면 **종료 코드 1**로 끝난다. 실행 오류가 아니라 고칠 것이 있다는 뜻이므로 JSON의 `fail`을 읽고 처리한다.
- 판정은 PASS·WARN·FAIL 외에 **INSUFFICIENT**(원문 확보율·원문 확인 인용 비율이 50% 미만 등 보증 불가)와 **N/A**(검사 대상 없음)가 있다. FAIL 0건이어도 INSUFFICIENT·N/A 항목은 검증된 것이 아니므로 사용자에게 그대로 알린다.
- V07은 09·11 본문 초안(`state/notes/*_body.md`)에서 UNVERIFIED·CONFLICT 근거를 인용하거나, 따옴표 직접 인용이 그 E-ID의 발췌와 다르면 FAIL이다. V06은 `state/text/`가 PDF에서 새로 추출한 텍스트와 다르면 FAIL이다(`$LRA evidence reverify --reextract`로 복구).
- **FAIL은 모두 고친 뒤** 다시 `render` → `verify`. 고칠 수 없으면(예: 원문 미확보) 사유를 보고서 Ⅶ에 적고 사용자에게 알린다.
- V05(자동 부분)는 Stage B·C 실행 여부, B·C의 출처 제한 여부, 미등록 출처 ID 사용, '출처 밖' 제외 사유, 우선 출처 밖 문헌의 선정 사유, coverage 기록을 점검한다.
- V16은 저장된 점수·순위를 기준 점수와 가중치로 다시 계산해 일치하는지, 근거 없는 점수·사유 없는 고정·인용지표를 근거로 쓴 흔적·미평가·이전 버전 기준 평가를 점검한다. V17(KCI/통합)은 실측 여부·KCI 검색 기록·중복 검토 대기·의심 병합·KCI 모드 혼합을 점검한다(INTERNATIONAL이면 N/A).
- 사람 평가 지표(V02·V03·V05 Recall·V09~V12, V16 전문가 일치, V17 병합 정확도)는 `미검증`으로 둔다. 임의 합격 처리 금지.
- 최종 보고: Core 편수(승인/임시), 상위 중요 문헌(1~5위)과 평가보류 문헌, 검색 모드·국내외 구성, Stage별 발견·선정 수, 우선 출처 밖 문헌 수, 전문 확보 a/b, 근거 검증상태 집계, API 호출·비용, FAIL/WARN 요약, 사용자 할 일(06 요청목록, 승인 대기 항목).

## 3. 절차 B — 연구 진행 중 피드백·증분 확장 (F11·F12·F13)

트리거: 연구계획서 v2 업로드, 데이터 분석 중 질문, 지도·심사 의견, 새 참고논문.

1. **기록:** `$LRA feedback add --kind plan_version|analysis_question|review_comment|new_paper --request "<요지>"` → F-ID.
   계획서 v2면 A1·A2처럼 `register-raw` → 새 profile JSON → `$LRA profile set --file ... --source raw/plan_v2.pdf` (diff 자동 계산).
2. **영향 식별:** diff에서 유지·신규·변경·삭제된 질문·변수·이론·방법·분야·장절을 정리.
3. **기존 근거로 충분한지 먼저 확인:** Evidence·카드·Core/Extended를 검토. 충분하면 **신규 검색 없이** 기존 근거로 답하고
   `$LRA feedback resolve --id F-001 --sufficiency "충분: E-004-02, E-011-01" --decision "추가 탐색 불필요"`.
4. **불충분하면** Gap 등록(§5) → **그 세부영역의 우선 출처를 다시 정한다**(기존 목록을 기계적으로 재사용하지 않음):
   `$LRA source find --query "…"` → `$LRA source add S… --gap G-003 --field "…" --reason "…"`
   → Stage A: `$LRA search --query "..." --purpose gap:G-003 --sources preferred --feedback F-001` (Gap 전용 목록이 자동 사용됨)
   → Stage B/C: `$LRA expand … --feedback F-001`, `$LRA search … --purpose gap:G-003 --feedback F-001` (제한 없음)
   → 선별 → `$LRA select P-045 --tier EXTENDED --roles methodology --reason "G-003 보완"`. **고정 편수(10/20)를 채우지 않는다.** 필요 편수와 근거를 gap의 `decision_basis`에 쓴다.
5. 제공받지 않은 데이터에 대해 통계 진단을 수행한 것처럼 말하지 않는다. 문헌이 말하는 범위만 답한다.
6. **변화가 크면** 기존 Seed·Core를 `유지 / 재분류 / 제외 후보 / 추가 검토`로 재평가해 표로 제시하고, **새 연구계획 버전 기준으로 중요도를 다시 평가**한다(`rank score`, 각 항목 `change_reason`에 변화 이유). 이전 버전 순위·승인은 그대로 보존되고 03a·13에 전후 점수·순위가 나온다. 새 순위는 `PROPOSED`로만 반영하며 승인된 Core를 자동 삭제하지 않는다. 검색 모드 변경(예: `KCI → INTEGRATED`)도 `mode set --reason`으로 기록하고 새 모드 후보의 영향을 별도로 평가한다. 연구분야가 바뀌었으면 기본 우선 출처 목록도 새 버전으로 다시 제안(`source add`는 현재 버전에 기록, 더 이상 쓰지 않을 출처는 `source retire`). 제외·교체는 사용자 승인 후 `deselect`(삭제 아님).
7. 추가 문헌에 A9~A12 적용(`fetch --papers ...`, 근거·카드) → `coverage --record` → 영향받는 notes만 수정 → `$LRA render` (13 포함) → `verify`.
   `$LRA feedback resolve --id F-001 --runs R-031,R-032 --added P-045,P-046 --decision "..."`; 사용자가 승인하면 `--approval APPROVED`.
8. 신규 문헌이 새 개념·방법·결과를 거의 더하지 못하거나 사용자 목표를 충족하면 **확장 종료를 제안**한다. 편수로 포화를 단정하지 않는다.

## 4. 사용자 승인 지점

| 지점 | 에이전트가 하는 것 | 사용자에게 받는 것 |
|---|---|---|
| ⓪ Profile·우선 학술출처 | 구조화·`미정` 표시·검색식, 분야별 출처·선정 이유·Source ID 제시(01·01a) | 분야·학술지·학회 우선순위 추가·수정·제외(선택) |
| ① Seed 3편 | 후보·근거·대안 제시(02) | 승인/교체. 미승인 진행 시 `임시선정` |
| ② Core | 중복·서지·철회·역할 점검, Stage별·출처 밖 현황과 함께 제안(03·04) | 최종 Core 승인 |
| 중요도 순위·검색 모드 | 잠정 점수·전체/역할별 순위·근거(03a), 모드별 범위·국내외 구성(01b) | 가중치 조정, 순위 고정(Pin), 국내외 비중, 모드 변경 승인 |
| 국내외 중복 | `REVIEW_DUPLICATE` 후보 비교 제시 | 불확실한 동일 연구 판정 |
| 원문 | OA만 다운로드, 실패 목록(06) | 권한 있는 PDF 제공(선택). 외부 계정·구독 자료 사용은 사전 승인 |
| 연구공백·활용계획 | 근거·한계·불확실성 표시 | 학술적 타당성·인용 반영 최종 판단 |
| 계획 변경 | diff·영향·재평가 표, Gap별 우선 출처 재설정 | Core 제외·교체 승인 |

## 5. 실패·재실행 규칙

| 상황 | 처리 |
|---|---|
| OpenAlex 429/예산 소진 | 중단하고 남은 단계와 키 설정 방법을 안내. 캐시된 결과로 가능한 작업은 계속 |
| 우선 출처 후보가 안 나옴 | 검색어를 분야 영문명으로 바꿔 `source find` 재시도. 그래도 없으면 Stage A를 건너뛰고 C로 시작(사유 기록) |
| Stage A 결과 0건·과소 | 출처 보완 또는 `--stage C --expansion-reason`으로 완화. 3회 실패 시 사용자에게 용어·분야 확인 |
| 역할 후보 없음 | 해당 Seed `없음/추가 탐색 필요`, 억지 지정 금지 |
| DOI 없음 | 자동 제외하지 않음. `NO_DOI`로 표시하고 OpenAlex ID로 추적 |
| 다운로드 403·HTML·제목 불일치 | 우회 시도 금지. 06에 기록(제목 불일치 파일은 `state/quarantine/`) |
| 인용 UNVERIFIED/CONFLICT | 원문에서 다시 확인해 수정, 불가하면 인용문 삭제 후 재서술만 |
| verify FAIL | 원인 수정 → render → verify 반복. 숨기지 않음 |
| 출처 ID가 비어 있음(구버전) | `$LRA refresh` |
| KCI 키 없음·실측 미통과 | KCI 검색 중단. 사용자에게 키 발급·`kci probe` 안내, 그동안 OpenAlex로 진행할지 묻는다 |
| KCI 오류("등록되지 않은 key", "검색 조건이 없습니다") | 키·검색어 확인. 제목어를 더 짧게 쪼개 재시도. 응답 형식이 명세와 다르면 probe 결과를 사용자에게 보고 |
| 중요도 근거 부족 | 해당 기준 빈칸 → 평가보류. 원문 확보 후 재평가 |
| 중단 후 재개 | `$LRA status`로 현재 단계를 판단해 이어서 진행(모든 상태는 DB에 있음) |

## 6. 산출물

`out/01`, `01a_우선학술출처_선정표.md`, `01b_국내외_검색모드_통합이력.md`(KCI/INTEGRATED 사용 시), `02`, `03`, `03a_논문별_중요도_순위표.md`, `04`~`13`(13은 피드백 발생 후), `14_참고문헌_내보내기.bib`(선택), `16_검증결과.md`.
`03`·`05`는 CSV(UTF-8 BOM, Excel 호환) — 03에는 중요도 순위·역할별 순위·점수·등급·근거, 국문/영문 제목, 검색DB·KCI ID, 출처·우선출처여부·발견Stage, DB별 피인용(보조, 순위 미반영)이 들어간다. 원문 PDF는 `library/Core|Extended/P-xxx_저자_연도.pdf` — 실제 연결은 P-ID와 SHA-256.

## 7. 명령 요약

| 명령 | 용도 |
|---|---|
| `init` `status` `register-raw` | 프로젝트·원본 고정 |
| `profile set/show` | Research Profile 버전 등록(diff 자동) |
| `source find/add/add-kci/exclude/retire/approve/list` | 분야별 우선 학술출처(Source ID·KCI 학술지 실재 확인, `--gap` 전용 목록) |
| `mode set/show` | 검색 모드 INTERNATIONAL / KCI / INTEGRATED |
| `kci probe` | KCI Open API 실측(인증키 필요) — 통과해야 KCI 검색 가능 |
| `search` | Stage A(`--sources preferred`) / Stage C(출처 제한 없음, 기본). `--db kci`로 KCI |
| `rank score/pin/unpin/approve/weights/show` | 중요도 점수·Core 전체·역할별 순위, 사용자 고정·승인·가중치 버전 |
| `expand` | Stage B 인용망(backward/forward/related), 출처 제한 불가 |
| `add-paper` | 사용자 추천(Stage U) |
| `coverage [--record]` | Stage A/B/C 비교·우선 출처 밖 발견·호출량 |
| `candidates` `show` `screen` | 후보 검토(★=우선 출처)·선별 기록 |
| `seed set/reject` `select` `deselect` `approve` | Seed·Core·Extended 선정/제외/승인 |
| `dedup [--review/--link/--distinct/--unlink]` `bibcheck` `integrity` `refresh` | 판본·국내외 중복·Crossref 서지 대조·정정/철회·메타데이터 보강 |
| `fetch` `ingest-fulltext` | OA 원문 다운로드·사용자 PDF 승격 |
| `evidence add/reverify` `card` `mapping` `gap` `feedback add/resolve` | 분석 결과 등록 |
| `render [01 01a 01b 02 03 03a … 13]` `export-bib` `verify` | 산출물·BibTeX·검증 |

공통 옵션(명령 **앞에** 둔다, 예: `$LRA --no-cache search ...`): `--root <프로젝트>` `--no-cache` `--all-types`(심사기록·정오표 등 비논문 레코드 포함).

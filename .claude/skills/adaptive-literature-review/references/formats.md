# 입력 JSON 형식 (에이전트 → 도구)

모든 JSON은 `state/work/` 아래에 저장한 뒤 `lra.py <명령> --file <경로>`로 등록한다. 등록 후 파일은 지워도 된다(DB에 기록됨).

## 1. Research Profile — `profile set --file`

```json
{
  "topic": "연구주제 한 줄",
  "research_fields": {"theory": ["혁신경제학"], "methodology": ["계량경제학(준실험)"], "empirical": ["과학기술정책"]},
  "source_preferences": {"prefer": ["사용자가 지목한 학술지·학회명"], "exclude": ["사용자가 제외 요청한 출처명 + 사유"]},
  "research_questions": ["RQ1 ...", "RQ2 ..."],
  "hypotheses": ["H1: ..."],                       
  "theories": ["이론명 또는 '미정'"],
  "key_concepts": [{"ko": "개방형 혁신", "en": "open innovation", "synonyms": ["inbound innovation"]}],
  "variables": [{"name": "R&D 협력", "role": "IV|DV|mediator|moderator|control", "definition": "..."}],
  "population": "연구대상 또는 '미정'",
  "setting": "국가·산업·기간 등 맥락",
  "methods": ["패널 회귀"],
  "data": "자료원 또는 '미정'",
  "plan_sections": [{"id": "1", "title": "서론"}, {"id": "2", "title": "이론적 배경"}],
  "undetermined": ["입력에서 확인되지 않아 확정하지 않은 항목"],
  "scope": {"years": "2010-", "languages": ["en", "ko"], "fields": [], "include": [], "exclude": []},
  "search_strategy_ko": {"empirical": ["연구개발 보조금", "R&D 보조금"], "independent": ["연구 개발 지원 효과"]},
  "search_strategy": {
    "theory": ["\"absorptive capacity\" AND innovation"],
    "methodology": ["\"difference-in-differences\" R&D subsidy"],
    "empirical": ["R&D subsidy firm innovation Korea"],
    "independent": ["public R&D additionality"]
  },
  "sensitive_terms_excluded": ["외부 검색어에서 뺀 비공개 용어(내용 말고 사유만)"]
}
```
- `research_fields`는 우선 학술출처(01a)를 분야별로 나눠 고르는 기준이다. `source_preferences`는 사용자가 말한 것만(없으면 비움) — 실제 반영은 `source add --origin user` / `source exclude`로 한다.
- 필수: `topic`, `research_questions`, `search_strategy`. 모르는 값은 `"미정"` + `undetermined`에 기록.
- `scope.exclude`는 사용자가 말한 조건만. 에이전트가 배제 조건을 만들지 않는다.
- 검색식은 OpenAlex 문법: 공백=AND, `OR`, `NOT`, `"구문"` 지원. 외부로 전송되므로 비공개 계획의 고유명사·미발표 수치는 넣지 않는다.

## 2. 근거 — `evidence add --file`

```json
[{
  "paper_id": "P-007",
  "type": "concept|theory|method|finding|statistic|limitation|definition",
  "claim": "이 근거가 뒷받침하는 주장(한 문장, 국문)",
  "verbatim_short_quote": "원문 그대로의 짧은 발췌(≤40단어). 원문 미확보면 빈 문자열",
  "paraphrase": "국문 재서술. 수치는 원문 단위·조건 그대로",
  "pdf_page": 5,
  "printed_page": "312",
  "section_or_table": "Table 3 / 4.2 Results"
}]
```
- `pdf_page`는 `state/text/P-007/p005.txt`의 5 — PDF 순번이다. 인쇄 쪽수는 확인한 경우에만 `printed_page`에.
- 검증상태는 도구가 원문과 글자 대조해 정한다. 에이전트가 `verification_status`를 넣어도 무시된다.
- 결과: `VERIFIED_FULLTEXT`(일치) / `CONFLICT`(다른 쪽에 있음 → 쪽수 수정 후 재등록) / `UNVERIFIED`(원문에 없음 → 인용문 수정 또는 삭제) / `ABSTRACT_BASED` / `METADATA_ONLY`.
- 같은 `evidence_id`를 넣으면 덮어쓴다(수정용).

## 3. 분석카드 — `card --file`

```json
[{
  "paper_id": "P-007",
  "research_overview": {"목적": "...", "연구질문": "...", "이론·가설": "..."},
  "methodology": {"연구유형": "준실험", "표본·기간": "...", "데이터": "...", "변수·측정": "...", "분석방법": "...", "식별전략": "..."},
  "key_findings": ["결과 서술 (E-007-02)", "..."],
  "quality_limitations": "추가 의견(선택)",
  "relevance": "본 연구 2.3절 H2의 이론 근거; 변수 X의 측정 근거",
  "open_questions": ["원문 미확보로 표 3 수치 미확인"],
  "quality": {
    "study_type": "준실험(DID)",
    "strengths": "...",
    "limitations": "저자 명시 한계 + 별도 검토 의견(구분 표기)",
    "risk_tags": ["selection-bias", "single-country"],
    "appraisal_status": "DRAFT(사람 검토 전)"
  }
}]
```
- 확인 못 한 필드는 비우거나 `"원문에서 확인되지 않음"`. 초록 기반 카드에는 쪽수·표 번호·직접 인용을 쓰지 않는다.
- `key_findings`의 수치·방향·유의성은 반드시 근거 ID(E-...)를 단다.
- 품질 점수(○점)나 등급을 매기지 않는다. 인용수·학술지 명성으로 품질을 판단하지 않는다.

## 4. 장절 활용 매핑 — `mapping --file`

```json
[{
  "section_id": "2.3", "section_title": "연구가설",
  "hypothesis_or_variable": "H2 / 변수: 흡수역량",
  "paper_id": "P-007", "evidence_id": "E-007-02",
  "use_purpose": "가설 방향의 근거|개념 정의|측정 근거|방법 정당화|반례·한계|논의 비교",
  "citation_mode": "재서술|직접인용",
  "drafting_guidance": "어떻게 서술할지 한두 문장",
  "limitation": "적용 시 주의(표본·맥락 차이 등)"
}]
```
- `직접인용`은 `VERIFIED_FULLTEXT` 근거에만. 승인 상태는 기본 `PENDING`.

## 5. 연구공백 — `gap --file`

```json
[{"current_evidence": "P-003, P-011이 대기업 표본만 다룸",
  "missing_scope": "중소기업 표본의 효과", "priority": "상|중|하",
  "planned_search": "검색식 또는 확장 방향", "decision_basis": "추가 편수를 정한 이유",
  "status": "OPEN|SEARCHING|ADDRESSED|DEFERRED"}]
```

## 6. 중요도 평가 — `rank score --file`

```json
[{
  "paper_id": "P-007",
  "research_fit": 5, "role_contribution": 4, "evidence_quality": null, "distinctiveness": 2, "time_relevance": 4,
  "rationale": {
    "research_fit": "H1(공공 R&D → 중소기업 특허)과 동일한 관계를 같은 대상으로 분석",
    "role_contribution": "실증 Seed 대안, 표본 구성·변수 정의를 그대로 참고 가능",
    "evidence_quality": "",
    "distinctiveness": "다른 Core와 같은 데이터원 사용",
    "time_relevance": "2021년, 최근 제도 개편 이후 자료"
  },
  "tier": "A",
  "uncertainty": "원문 미확보 — 효과크기 미확인",
  "change_reason": "재평가일 때만: 무엇이 바뀌어 점수가 달라졌는지"
}]
```
- 점수는 0~5 정수 또는 소수. **판단 근거가 없는 기준은 `null`** → 해당 논문은 `평가보류`(점수·순위 없음).
- 점수를 준 기준에는 반드시 근거 문장이 있어야 등록된다. 인용수·학술지 지표·KCI 등재를 근거로 쓰지 않는다.
- `tier`: `A`(핵심 활용) / `B`(적극 참고) / `C`(보완 참고). 고정 점수구간이 아니다.
- Core/Extended(제외 아님) 문헌만 평가된다. 점수·순위는 도구가 계산한다(에이전트가 순위를 직접 쓰지 않음).

가중치 변경 — `rank weights --file w.json --reason "..." --by user`:
```json
{"research_fit": 0.35, "role_contribution": 0.25, "evidence_quality": 0.20, "distinctiveness": 0.10, "time_relevance": 0.10}
```
합이 1이어야 한다. 새 가중치 버전(W2…)이 생기고 현행 평가가 재계산된다(기준 점수는 유지, 이전 기록 보존).

## 7. 서술 초안 — `state/notes/`

| 파일 | 렌더 결과 |
|---|---|
| `09_body.md` | `out/09_선행연구_비교분석.md` (+ 인용 문헌 목록 자동) |
| `11_body.md` | `out/11_선행연구_종합보고서.md` (+ Ⅷ 참고문헌·검증 현황·부록 자동). 본문의 `{{RANKING_TABLE}}` 자리에 Core 중요도 순위표가 들어간다 |
| `01.md`, `01a.md`, `01b.md`, `02.md`, `03a.md`, `04.md`, `10.md`, `12.md`, `13.md` | 해당 산출물 끝에 '검토 메모'로 추가 |

인용 표기: 본문에 `[P-007]` 또는 `[P-007; E-007-02]`. 렌더 시 `(Kim et al., 2021 [P-007; E-007-02])`로 바뀐다.
없는 ID, 다른 논문의 근거 ID는 ⚠ 표시되고 `verify`에서 FAIL.

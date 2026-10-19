# CLAUDE.md
- 이 폴더는 선행연구 조사·종합분석 에이전트 프로젝트다. 문헌조사 요청은 `.claude/skills/adaptive-literature-review/SKILL.md` 절차를 따른다.
- 입력 자료는 `raw/`에 있고, `raw/`의 원본은 수정·이동·삭제하지 않는다.
- 논문·DOI·인용문·쪽수·수치는 도구(`lra.py`)가 외부 DB나 원문에서 가져온 것만 쓰고, 확인되지 않으면 `확인 필요`로 남긴다.
- 결과물은 `out/`에 만들고, 상태·작업 파일은 `state/`에 둔다. 연구 자료(`raw/ state/ library/ out/`)는 커밋하지 않는다.
- 끝났다고 보고하기 전에 `lra.py verify`를 실행해 `out/16_검증결과.md`의 FAIL이 0건인지 확인한다(FAIL이 있으면 종료 코드 1). INSUFFICIENT·N/A 항목은 검증되지 않은 것이므로 보고에 그대로 적는다.

# 회사·발행사 이름과 그룹 관리

`data/dictionary/company_registry.json`이 대표 회사명, 회사 ID, 별칭, 소속 그룹의
단일 관리 파일이다. 기존 `group_whitelist.json`의 128개 명시적 항목과 신한은행을
초기값으로 넣었다. 기존 목록의 소속 최신성을 별도 조사한 것은 아니다.
이전 `group_whitelist.json`과 `issuer_group_rules.json`은 삭제했으며
현재 그래프 생성 코드에서는 읽지 않는다. 수정은 새 대응표에 한다.

```json
{
  "id": "Company_신한은행",
  "name": "신한은행",
  "aliases": ["(주)신한은행", "주식회사 신한은행"],
  "group": "신한"
}
```

- 별칭은 **같은 법인**의 다른 이름만 등록한다. 계열사를 별칭으로 합치지 않는다.
- `id`는 안정적인 식별자다. 대표 표시명을 변경해도 ID는 유지한다.
- 그룹은 확인한 소속만 입력한다. 미확인은 `null`로 둔다.
- 법인 표기·공백·브랜드 표기는 `src/graph/company_identity.py`에서 공통 정규화한다.
  기존 한글/영문 브랜드 표기 통일은 유지하지만, 브랜드 접두로 그룹을 추정하지 않는다.
- 미등록 기존 상장회사 ID는 유지한다. 미등록 신규 발행사는 정규화명으로
  `Company_...` ID를 생성한다. 별칭/정규화/ID 충돌은 임의 병합 대신 오류로 알린다.
- 미등록 발행사를 나중에 대응표에 추가할 때는 `unmapped_issuers.json`에 기록된
  ID를 그대로 사용한다. 그룹이나 표시명 변경 때문에 ID를 새로 만들지 않는다.
- `PD_PBCM` 우선, 빈 값만 상품명 파싱으로 보완한다. 그룹 미확인 여부와 무관하게
  발행사 노드와 `issuedBy`를 생성한다. 미매칭을 비상장의 근거로 사용하지 않는다.
- 그룹 미확인 발행사는 `data/processed/unmapped_issuers.json`에 이름과 고유 채권
  코드를 기록한다. 기존 접두 규칙으로만 분류됐던 회사는 검토 후 대응표에 추가한다.

## 로컬 재생성

프로젝트 루트에서 아래 순서대로 실행한다. 기존 생성 파일은 덧붙이지 않고 교체한다.
이 작업은 DuckDB를 수정하지 않는다. 기존 `instances_theme.ttl`은 유지한다.

```powershell
python -m src.graph.build_company_nodes
python -m src.graph.build_ttl
python -m src.graph.build_group
```

1. `company_nodes.json`과 `code_to_company.json`에 최신 회사 ID·정규화 기준을 저장한다.
2. 회사 노드, ETF 보유 관계·비중, 기존 회사와 매칭된 채권 발행 관계를 생성한다.
3. 독립 발행사와 채권 연결, 명시적인 그룹 소속, 미확인 발행사 검토 목록을 생성한다.

세 단계가 모두 성공한 뒤 서버를 재시작한다. rdflib의 기존 Graph에 새 TTL을
추가하는 방식은 사용하지 않는다. 새 Graph에 파일을 로드해야 이전 `Issuer_` 노드가
남지 않으며 QueryBuilder의 회사 이름 인덱스도 갱신된다.

## 검증

```powershell
python -m unittest discover -s tests -v
```

테스트는 임시 Excel·JSON, 메모리 그래프·DB만 사용한다. 실제 그래프 재생성 후에는
원본 신한은행 발행사 행의 고유 `PD_NO` 집합과 `Company_신한은행`의 발행 채권 집합을
비교한다. 행 수와 고유 채권 수는 다를 수 있다. 기존 SK하이닉스 연결과 ETF 보유
연결도 함께 비교한다. 현재 저장된 SK하이닉스 TTL은 17행, 고유 채권 16개였다.

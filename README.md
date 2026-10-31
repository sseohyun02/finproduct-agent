# fin-013
AI Festival 2026 - 누보

# 금융상품 Agent (2026 미래에셋 AI Festival, 주제1)

국내채권·국내ETF·해외ETF·공모펀드 4종 마스터 데이터에 대한 자연어 질의응답 AI Agent.
지식그래프(RDF/SPARQL) + 관계형(DuckDB SQL) 하이브리드 구조로, LLM(HyperCLOVA X)은
자연어 이해(의도추출·답변생성)만 담당하고 쿼리 생성은 결정론적 코드가 수행하여 환각을 억제한다.

## 시스템 구성

```
질문 → [1] unanswerable 게이트 → [2] CLOVA 의도추출(질문→의도 JSON)
     → [3] 쿼리빌더(의도→SQL/SPARQL, 결정론적) → [4] 실행(DuckDB/rdflib)
     → [5] CLOVA 답변생성 → 응답(JSON)
```

- 지식그래프: 회사·테마·그룹·보유(Holding) 노드로 다중홉 관계 질의 처리
  (예: "삼성전자와 같은 그룹 계열사가 담긴 반도체 테마 ETF")
- SQL: 속성 필터·정렬·집계 (위험등급, 보수, 수익률, 순자산 등)
- 그래프 결과(상품코드)를 SQL 조건과 결합해 최종 답변 생성

## 요구 환경

- Python 3.12
- 주요 패키지: fastapi, uvicorn, duckdb, rdflib, openai(호환 클라이언트), python-dotenv
- LLM: HyperCLOVA X (HCX-005), NCP CLOVA Studio OpenAI 호환 엔드포인트

## 실행 방법

### 방법 1: Docker (권장, 재현용)

```bash
# 1. 이미지 빌드
docker build -t fin-agent .

# 2. 실행 (CLOVA API 키는 실행 시 주입 - 이미지에 포함되지 않음)
docker run -p 8000:8000 -e CLOVA_API_KEY=발급받은_키 -e CLOVA_MODEL=HCX-005 fin-agent
```

### 방법 2: 로컬 직접 실행

```bash
# 1. 가상환경 + 패키지
python -m venv fin
source fin/bin/activate        # Windows: fin\Scripts\activate
pip install -r requirements.txt

# 2. .env 생성 (프로젝트 루트)
#    CLOVA_API_KEY=발급받은_키
#    CLOVA_MODEL=HCX-005

# 3. 실행
export AGENT_BASE=$(pwd)        # Windows: set AGENT_BASE=%cd%
cd src/agent
uvicorn app:app --host 0.0.0.0 --port 8000
```

## API 명세

### GET /answer

| 파라미터 | 필수 | 설명 |
|---|---|---|
| question_id | 선택 | 질문 식별자 (평가용) |
| question | 필수 | 자연어 질문 |

**응답 (JSON)**

| 필드 | 설명 |
|---|---|
| question_id | 요청받은 질문 식별자 |
| question | 질문 원문 |
| retrieved_context | 답변 근거 (실행 SQL·조회 결과) |
| think_trace | 추론 과정 (의도추출→그래프/SQL 경로) |
| answer | 최종 자연어 답변 |

**예시**

```bash
curl "http://localhost:8000/answer?question_id=Q-001&question=위험등급 5 이상 채권"
```

### GET /health

서버 상태 확인. `{"status":"ok","agent_ready":true}` 반환.

## 데이터

- `data/processed/findb.duckdb` : 4종 마스터 (DuckDB, 실행 시 read-only)
- `data/graph/*.ttl` : 지식그래프 인스턴스 (회사·테마·그룹)
- `ontology.ttl` : 온톨로지 스키마
- `data/dictionary/*.json` : 개념 사전, unanswerable 패턴

회사·발행사 ID와 그룹 소속은 `data/dictionary/company_registry.json`에서 관리한다.
대응표 편집 원칙과 JSON·TTL 재생성 순서는 [회사 식별 관리 안내](docs/company_identity.md)를 참고한다.

데이터 기준일: 2026-07-11 스냅샷. 평가 시 기준 데이터는 주최 측 제공 데이터를 우선한다.

## 디렉터리 구조

```
fin-013/
├── Dockerfile, requirements.txt, README.md
├── ontology.ttl
├── data/
│   ├── processed/findb.duckdb
│   ├── graph/instances_{company,theme,group}.ttl
│   └── dictionary/{concepts,unanswerable}.json
└── src/agent/
    ├── app.py              # FastAPI 엔드포인트
    ├── agent_pipeline.py   # 파이프라인 오케스트레이션
    ├── query_builder.py    # 의도→SQL/SPARQL (결정론적)
    ├── clova_client.py     # HyperCLOVA X 클라이언트
    └── prompts/intent_prompt.md
```

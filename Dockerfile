# =========================================================
# 금융상품 Agent (2026 미래에셋 AI Festival)
# 재현 가능한 실행 환경 - 코드 + 데이터(DuckDB·그래프TTL) 포함
# CLOVA API 키는 이미지에 넣지 않고 실행 시 -e 로 주입
# =========================================================
FROM python:3.12-slim

# 작업 디렉터리
WORKDIR /app

# 의존성 먼저 설치(레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 프로젝트 전체 복사 (.dockerignore로 .env·venv 등 제외)
COPY . .

# 그래프·DB 경로 기준(프로젝트 루트)
ENV AGENT_BASE=/app

# API 포트
EXPOSE 8000

# 서버 실행 (app.py 위치: src/agent)
WORKDIR /app/src/agent
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
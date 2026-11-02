"""프로젝트 전역 설정: 경로, 상수, 테이블 정의"""
from pathlib import Path
import os

# 프로젝트 루트 (config.py 기준 2단계 상위: src/config.py → financial-agent/)
ROOT = Path(os.environ.get("AGENT_BASE") or Path(__file__).resolve().parents[1]).resolve()

# 데이터 경로
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"              # 원본 엑셀
PROCESSED_DIR = DATA_DIR / "processed"  # DuckDB 파일
DICT_DIR = DATA_DIR / "dictionary"      # 개념사전 JSON

# DuckDB 파일 경로
DB_PATH = PROCESSED_DIR / "findb.duckdb"

GRAPH_DIR = DATA_DIR / "graph"

# 테이블 정의 (테이블코드)
TABLES = {
    "bond":         {"code": "PRBD01N001", "kr": "국내채권마스터"},
    "etf_domestic": {"code": "PREF01N001", "kr": "국내ETF마스터"},
    "etf_overseas": {"code": "PREF02N001", "kr": "해외ETF마스터"},
    "fund":         {"code": "PRFD01N001", "kr": "공모펀드마스터"},
}

# 각 테이블의 기대 건수 (적재 검증용)
# [2026-08 갱신] 대회측 새 데이터 반영:
#   채권·펀드가 정제되어 행수 감소(만기 지난 채권 제거, 펀드 정리), ETF는 소폭 증가.
EXPECTED_ROWS = {
    "bond": 21882,
    "etf_domestic": 1780,
    "etf_overseas": 6037,
    "fund": 23676,
}

def find_raw_file(code_or_name):
    exact = RAW_DIR / code_or_name
    if exact.is_file():
        return str(exact)
    token = code_or_name.split("_")[0].lower()
    candidates = [p for p in RAW_DIR.glob("*.xlsx")
                  if not p.name.startswith(("~$", "._")) and "schema" not in p.name.lower()
                  and token in p.name.lower()]
    candidates.sort(key=lambda p: (not p.name.lower().endswith("_data.xlsx"), len(p.name), p.name))
    if not candidates:
        raise FileNotFoundError(f"{RAW_DIR}: {code_or_name} 원본 파일이 없습니다.")
    return str(candidates[0])

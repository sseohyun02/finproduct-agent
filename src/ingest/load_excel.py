"""원본 엑셀 4종을 DuckDB에 적재한다.

원본은 수정하지 않고, 읽어서 DuckDB 테이블로 만든다.
파일은 테이블코드로 찾는다.

[2026-08 갱신] 대회측 새 데이터 반영:
  - 파일명 형식이 '..._datarows.xlsx' → '..._data.xlsx'로 변경됨
    (구/신 파일명 모두 인식하도록 매칭 조건 완화)
  - 컬럼명이 소문자로 통일됨(특히 채권). 적재는 원본 컬럼명을 그대로 보존한다.
  - 행 수가 바뀜(채권/펀드 정제). EXPECTED_ROWS 불일치는 오류가 아니라 경고로만 표시.
"""
import sys
import pandas as pd
import duckdb

from src.config import (
    RAW_DIR, PROCESSED_DIR, DB_PATH, TABLES, EXPECTED_ROWS,
)


def find_datarows_file(code: str):
    """RAW_DIR에서 해당 테이블코드의 데이터 엑셀 파일을 찾는다.

    파일명에 테이블코드가 들어있고, 'data'가 포함되며 'schema'가 아닌 파일을 찾는다.
    - 신규 형식: prbd01n001_data.xlsx
    - 구 형식:   PRBD01N001_국내채권마스터_20260711_datarows.xlsx
    둘 다 매칭되도록 대소문자 무시하고 'data' 포함 / 'schema' 제외로 판단.
    """
    code_low = code.lower()
    candidates = []
    for p in RAW_DIR.glob("*.xlsx"):
        name = p.name.lower()
        if name.startswith("~$") or name.startswith("._"):
            continue  # 임시/메타 파일 제외
        if code_low in name and "data" in name and "schema" not in name:
            candidates.append(p)

    if not candidates:
        raise FileNotFoundError(
            f"[{code}] 데이터 엑셀을 {RAW_DIR}에서 찾지 못했습니다. "
            f"(파일명에 '{code}'와 'data'가 포함되어야 함)"
        )
    if len(candidates) > 1:
        # 신규 형식(_data.xlsx)을 우선
        candidates.sort(key=lambda p: (0 if p.name.lower().endswith("_data.xlsx") else 1,
                                       len(p.name)))
        print(f"경고: [{code}] 후보 파일이 여러 개입니다. 사용: {candidates[0].name}")
    return candidates[0]


def load_all():
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))

    for table, meta in TABLES.items():
        code = meta["code"]
        path = find_datarows_file(code)
        print(f"[{table}] 적재 중: {path.name}")

        # 엑셀 → pandas → DuckDB (원본 컬럼명 그대로 보존)
        df = pd.read_excel(path)
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM df")

        # 건수 검증 (불일치는 경고만 — 새 데이터로 행수가 달라진 것은 정상)
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        expected = EXPECTED_ROWS.get(table)
        if expected is None:
            status = "기대값 미설정"
        elif n == expected:
            status = "OK"
        else:
            status = f"행수 변경됨(기대 {expected} → 실제 {n})"
        print(f"  → {n}건 적재 완료 [{status}], 컬럼 {len(df.columns)}개")

    con.close()
    print(f"\n완료: {DB_PATH}")


if __name__ == "__main__":
    try:
        load_all()
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)
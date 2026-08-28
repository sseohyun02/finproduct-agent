"""원본 엑셀 4종을 DuckDB에 적재한다.

원본은 수정하지 않고, 읽어서 DuckDB 테이블로 만든다.
파일은 테이블코드로 찾는다.
"""
import sys
import pandas as pd
import duckdb

from src.config import (
    RAW_DIR, PROCESSED_DIR, DB_PATH, TABLES, EXPECTED_ROWS,
)


def find_datarows_file(code: str):
    """RAW_DIR에서 해당 테이블코드의 datarows 엑셀 파일을 찾는다."""
    candidates = [
        p for p in RAW_DIR.glob("*.xlsx")
        if code in p.name and "datarows" in p.name.lower()
    ]
    if not candidates:
        raise FileNotFoundError(
            f"[{code}] datarows 엑셀을 {RAW_DIR}에서 찾지 못했습니다."
        )
    if len(candidates) > 1:
        print(f"경고: [{code}] 후보 파일이 여러 개입니다. 첫 번째 사용: {candidates[0].name}")
    return candidates[0]


def load_all():
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))

    for table, meta in TABLES.items():
        code = meta["code"]
        path = find_datarows_file(code)
        print(f"[{table}] 적재 중: {path.name}")

        # 엑셀 → pandas → DuckDB
        df = pd.read_excel(path)
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM df")

        # 건수 검증
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        expected = EXPECTED_ROWS.get(table)
        status = "OK" if n == expected else f"주의(기대 {expected})"
        print(f"  → {n}건 적재 완료 [{status}], 컬럼 {len(df.columns)}개")

    con.close()
    print(f"\n완료: {DB_PATH}")


if __name__ == "__main__":
    try:
        load_all()
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)
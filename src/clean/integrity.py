"""행 무결성 검사

값이 통째로 밀리거나 깨져서 행 전체를 신뢰할 수 없는 행을 찾아
row_valid = false 로 표시한다. (삭제하지 않고 플래그만 단다)

발견된 손상 행 (직접 데이터 점검으로 확인):
- fund: 위험등급명이 정상 6종도 NULL도 아닌 행 (예: '06').
        원본 엑셀에서 값 안의 따옴표로 인해 칸이 밀린 것으로 추정. 1건.
"""
import duckdb
from src.config import DB_PATH

# fund 위험등급명의 정상값 (공백 표기 차이 포함)
FUND_RISK_NAME_VALID = [
    "매우 높은 위험", "높은 위험", "다소 높은 위험",
    "보통 위험", "낮은 위험", "매우 낮은 위험",
    "매우높은위험", "높은위험", "다소높은위험",
    "보통위험", "낮은위험", "매우낮은위험",
]


def apply_integrity(con: duckdb.DuckDBPyConnection):
    """각 테이블에 row_valid 플래그를 추가한다. 기본 true, 손상 행만 false."""

    # --- fund ---
    _add_column_if_absent(con, "fund", "row_valid", "BOOLEAN")
    # 일단 전부 유효로 초기화
    con.execute("UPDATE fund SET row_valid = true")
    # 손상 조건: 위험등급명이 NULL도 아니고 정상값 목록에도 없는 행
    valid_list = ", ".join(f"'{v}'" for v in FUND_RISK_NAME_VALID)
    con.execute(f"""
        UPDATE fund SET row_valid = false
        WHERE zrin_fd_ivst_risk_grd_nm IS NOT NULL
          AND zrin_fd_ivst_risk_grd_nm NOT IN ({valid_list})
    """)
    n_bad = con.execute(
        "SELECT COUNT(*) FROM fund WHERE row_valid = false"
    ).fetchone()[0]
    print(f"[fund] 손상 행 {n_bad}건 표시 (row_valid=false)")

    # 다른 테이블은 현재 행 손상 미발견 → 전부 유효로 표시
    for table in ["bond", "etf_domestic", "etf_overseas"]:
        _add_column_if_absent(con, table, "row_valid", "BOOLEAN")
        con.execute(f"UPDATE {table} SET row_valid = true")
        print(f"[{table}] 손상 행 없음 (전체 유효)")


def _add_column_if_absent(con, table, column, dtype):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")


if __name__ == "__main__":
    con = duckdb.connect(str(DB_PATH))
    apply_integrity(con)
    con.close()
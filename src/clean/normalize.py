"""기타 정규화: 날짜 변환 + 통화 표준화 + 상품명 공백 정리

세 가지 자잘한 정제를 모은다.
  1) 날짜: YYYYMMDD 숫자/문자로 저장된 날짜를 DATE 타입 새 컬럼(_date)으로 변환
           원본은 보존. 값 0이나 파싱 실패는 NULL.
  2) 통화: bond.CURR_CD의 '000'(유효하지 않은 통화)을 표준 컬럼에서 NULL 처리
  3) 공백: 상품명 뒤 공백을 원본에서 제거 (정보 손실 없는 단순 정리)
"""
import duckdb
from src.config import DB_PATH

# 테이블별 날짜 컬럼 목록 (형식 무관 — TRY_STRPTIME이 알아서 처리)
DATE_COLS = {
    "bond": ["ISU_DT", "MAT_DT", "CRD_GRD_DT", "PD_STD_INFO_UPDATE"],
    "etf_domestic": ["cu_upt_dt", "pd_lste_dt", "pd_lstg_dt", "wu_upt_dt", "du_upt_dt"],
    "etf_overseas": ["cu_upt_dt", "du_upt_dt", "pd_lstg_dt", "wu_upt_dt",
                     "du_clpr_base_dt", "du_nav_base_dt"],
}

# 상품명 컬럼 (뒤 공백 제거 대상)
NAME_COLS = {
    "bond": "PD_NM",
    "fund": "itm_nm",
}


def _date_convert_expr(col: str) -> str:
    """숫자(YYYYMMDD.0)든 문자('2026-06-15 00:00:00')든 DATE로 변환하는 표현식.

    - 값이 0 또는 파싱 불가면 NULL (TRY_ 계열이 실패를 NULL로 처리)
    - 숫자형: BIGINT로 만든 뒤 문자열화 → YYYYMMDD 파싱
    - 이미 ISO 문자열이면 앞 10자('2026-06-15')만 잘라 DATE 캐스팅
    """
    return f"""
        COALESCE(
            TRY_STRPTIME(CAST(TRY_CAST({col} AS BIGINT) AS VARCHAR), '%Y%m%d')::DATE,
            TRY_CAST(LEFT(CAST({col} AS VARCHAR), 10) AS DATE)
        )
    """


def apply_normalize(con: duckdb.DuckDBPyConnection):
    # ── 1) 날짜 변환: 각 컬럼마다 _date 새 컬럼 생성 ──
    for table, cols in DATE_COLS.items():
        for col in cols:
            new_col = f"{col}_date"
            _add_column_if_absent(con, table, new_col, "DATE")
            con.execute(
                f"UPDATE {table} SET {new_col} = ({_date_convert_expr(col)})"
            )
        print(f"[{table}] 날짜 {len(cols)}개 컬럼 → _date 변환 완료")

    # ── 2) 통화 표준화: bond.CURR_CD의 '000'을 NULL로 (표준 컬럼) ──
    _add_column_if_absent(con, "bond", "curr_cd_std", "VARCHAR")
    con.execute("""
        UPDATE bond
        SET curr_cd_std = CASE
            WHEN CURR_CD IN ('KRW','USD','EUR','JPY','CNY','GBP','AUD') THEN CURR_CD
            ELSE NULL   -- '000' 등 유효하지 않은 통화는 NULL
        END
    """)
    n_null = con.execute(
        "SELECT COUNT(*) FROM bond WHERE curr_cd_std IS NULL AND CURR_CD IS NOT NULL"
    ).fetchone()[0]
    print(f"[bond] 통화 표준화 완료. 무효 통화 NULL 처리: {n_null}건")

    # ── 3) 상품명 뒤 공백 제거 (원본 직접 TRIM, 정보 손실 없음) ──
    for table, col in NAME_COLS.items():
        before = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} != TRIM({col})"
        ).fetchone()[0]
        con.execute(f"UPDATE {table} SET {col} = TRIM({col})")
        print(f"[{table}] {col} 뒤공백 제거: {before}건")


def _add_column_if_absent(con, table, column, dtype):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")


if __name__ == "__main__":
    con = duckdb.connect(str(DB_PATH))
    apply_normalize(con)
    con.close()
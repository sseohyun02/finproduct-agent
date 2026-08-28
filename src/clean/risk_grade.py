"""위험등급 정제

3개 테이블(bond, etf_domestic, fund)의 서로 다른 형식의 위험등급을
공통 컬럼 risk_grade_num(1~6 정수), risk_grade_valid(불리언)로 통일한다.
etf_overseas는 위험등급 컬럼이 없어 제외한다.

방향성: 1등급=최고위험, 6등급=최저위험 (숫자 클수록 안전)
원본 컬럼은 수정하지 않고 새 컬럼만 추가한다.
"""
import duckdb
from src.config import DB_PATH

# 테이블별 위험등급 정제 SQL 조각.
# 각 테이블에서 risk_grade_num을 뽑아내는 표현식(원본 → 1~6 또는 NULL)
RISK_EXPR = {
    # bond: PD_RISK_GCD가 1~6이면 사용, 그 외(0 등)는 NULL
    "bond": """
        CASE WHEN PD_RISK_GCD BETWEEN 1 AND 6
             THEN CAST(PD_RISK_GCD AS INTEGER) END
    """,
    # etf_domestic: "매우높은위험(1등급)"에서 괄호 앞 숫자 추출
    "etf_domestic": r"""
        CAST(regexp_extract(pd_risk_nm, '([1-6])등급', 1) AS INTEGER)
    """,
    # fund: zrin_fd_ivst_risk_gcd가 1~6이면 사용, 그 외(20054, NULL)는 NULL
    "fund": """
        CASE WHEN zrin_fd_ivst_risk_gcd BETWEEN 1 AND 6
             THEN CAST(zrin_fd_ivst_risk_gcd AS INTEGER) END
    """,
}


def apply_risk_grade(con: duckdb.DuckDBPyConnection):
    """각 테이블에 risk_grade_num, risk_grade_valid 컬럼을 추가한다."""
    for table, expr in RISK_EXPR.items():
        # 1) risk_grade_num 컬럼 추가 (이미 있으면 건너뜀)
        _add_column_if_absent(con, table, "risk_grade_num", "INTEGER")
        con.execute(f"UPDATE {table} SET risk_grade_num = ({expr})")

        # 2) risk_grade_valid: num이 1~6이면 true
        _add_column_if_absent(con, table, "risk_grade_valid", "BOOLEAN")
        con.execute(f"""
            UPDATE {table}
            SET risk_grade_valid = (risk_grade_num BETWEEN 1 AND 6)
        """)

        # 검증 출력
        n_valid = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE risk_grade_valid"
        ).fetchone()[0]
        n_total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"[{table}] risk_grade 정제 완료: 유효 {n_valid}/{n_total}")


def _add_column_if_absent(con, table, column, dtype):
    """컬럼이 없으면 추가 (재실행 안전성)."""
    cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")


if __name__ == "__main__":
    con = duckdb.connect(str(DB_PATH))
    apply_risk_grade(con)
    con.close()
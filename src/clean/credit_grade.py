"""신용등급 정제 (채권)

채권의 개별 신용등급(crd_grd)을 정규화하고 순위 숫자로 매핑한다.
원본 컬럼은 수정하지 않고 아래 2개 컬럼만 추가한다.
  - bond_credit_grade : 채권 등급 (표기 정규화: AA0 → AA)
  - bond_credit_rank  : 채권 등급 순위 숫자 (비교·정렬용, 작을수록 안전, AAA=1)

표기 정규화 근거: 공식 표준 표기는 'AA'이며 'AA0'은 crd_grd 컬럼만의 표기다.
따라서 crd_grd의 끝 '0'만 제거해 표준 표기로 맞춘다. (+/- 부호는 건드리지 않음)
원본 crd_grd의 'AA0'은 그대로 두고, 정규화는 새 컬럼에서만 한다.

[2026-08 갱신] 대회측 새 데이터 반영:
  - 컬럼명이 소문자로 변경(CRD_GRD → crd_grd, PD_NO → pd_no).
  - 발행사 신용등급 컬럼(PD_EVCO_CRD_GRD)이 새 데이터에서 삭제됨.
    → 발행사 등급(issuer_credit_grade/split) 생성 로직을 제거한다.
    (실제 질의에서 발행사 등급은 사용하지 않았고, 채권 등급만 사용함)
  - crd_grd 값 형식(AAA, AA0, A0, C0 등)은 동일하여 정규화 로직 그대로 유효.
"""
import duckdb
from src.config import DB_PATH

# 신용등급 순위 (숫자 작을수록 상위=안전).
# 표준 표기 기준. 'AA0' 같은 변형은 정규화 후 매핑되므로 넣지 않는다.
CREDIT_RANK = {
    "AAA": 1,
    "AA+": 2, "AA": 3, "AA-": 4,
    "A+": 5, "A": 6, "A-": 7,
    "BBB+": 8, "BBB": 9, "BBB-": 10,
    "BB+": 11, "BB": 12, "BB-": 13,
    "B+": 14, "B": 15, "B-": 16,
    "CCC": 17, "CC": 18, "C": 19, "D": 20,
}


def apply_credit_grade(con: duckdb.DuckDBPyConnection):
    """bond에 채권 등급 관련 2개 컬럼을 추가한다."""

    # 순위표 임시 테이블
    con.execute("CREATE OR REPLACE TEMP TABLE grade_rank(grade VARCHAR, rk INTEGER)")
    con.executemany("INSERT INTO grade_rank VALUES (?, ?)", list(CREDIT_RANK.items()))

    _add_column_if_absent(con, "bond", "bond_credit_grade", "VARCHAR")
    _add_column_if_absent(con, "bond", "bond_credit_rank", "INTEGER")

    # ── 채권 등급(채권신용등급): crd_grd 표기 정규화 + 순위 ──
    # 끝의 '0' 하나만 제거 (AA0 → AA). +/- 는 유지. 원본 crd_grd는 안 건드림.
    con.execute("""
        UPDATE bond
        SET bond_credit_grade = regexp_replace(crd_grd, '0$', '')
        WHERE crd_grd IS NOT NULL
    """)
    # 정규화된 등급으로 순위 숫자 매핑
    con.execute("""
        UPDATE bond
        SET bond_credit_rank = grade_rank.rk
        FROM grade_rank
        WHERE bond.bond_credit_grade = grade_rank.grade
    """)

    # ── 검증 출력 ──
    n_bond = con.execute(
        "SELECT COUNT(*) FROM bond WHERE bond_credit_grade IS NOT NULL"
    ).fetchone()[0]
    n_rank_null = con.execute(
        "SELECT COUNT(*) FROM bond WHERE bond_credit_grade IS NOT NULL AND bond_credit_rank IS NULL"
    ).fetchone()[0]
    print(f"[bond] 채권 등급 {n_bond}건 (순위 미매핑 {n_rank_null}건)")


def _add_column_if_absent(con, table, column, dtype):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")


if __name__ == "__main__":
    con = duckdb.connect(str(DB_PATH))
    apply_credit_grade(con)
    con.close()
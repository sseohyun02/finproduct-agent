"""신용등급 정제 (채권)

bond의 신용등급은 두 컬럼에 나뉘어 있으며, 공식적으로 서로 다른 평가다.
  - PD_EVCO_CRD_GRD : 기업신용등급(발행사). "이 회사가 얼마나 튼튼한가".
                       국내 신용평가 3사 등급을 콤마로 나열 (예: "AAA, AAA, AAA")
  - CRD_GRD          : 채권신용등급(개별 채권). "이 채권 자체가 얼마나 안전한가".
                       후순위채 등은 발행사 등급보다 낮음.

두 등급은 의미가 달라 통일하지 않고 각각 별도 컬럼으로 보존한다.
원본 컬럼은 수정하지 않고 아래 4개 컬럼만 추가한다.
  - issuer_credit_grade : 발행사 대표 등급 (평가사 간 갈리면 가장 낮은 등급)
  - issuer_credit_split : 발행사 등급이 평가사 간 갈렸는지 여부
  - bond_credit_grade   : 채권 등급 (표기 정규화: AA0 → AA)
  - bond_credit_rank    : 채권 등급 순위 숫자 (비교·정렬용)

표기 정규화 근거: 공식 표준 표기는 'AA'이며 'AA0'은 CRD_GRD 컬럼만의 표기다.
따라서 CRD_GRD의 끝 '0'만 제거해 표준 표기로 맞춘다. (+/- 부호는 건드리지 않음)
원본 CRD_GRD의 'AA0'은 그대로 두고, 정규화는 새 컬럼에서만 한다.
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
    """bond에 발행사 등급/채권 등급 관련 4개 컬럼을 추가한다."""

    # 순위표 임시 테이블 (콤마 분리 등급 비교용)
    con.execute("CREATE OR REPLACE TEMP TABLE grade_rank(grade VARCHAR, rk INTEGER)")
    con.executemany("INSERT INTO grade_rank VALUES (?, ?)", list(CREDIT_RANK.items()))

    _add_column_if_absent(con, "bond", "issuer_credit_grade", "VARCHAR")
    _add_column_if_absent(con, "bond", "issuer_credit_split", "BOOLEAN")
    _add_column_if_absent(con, "bond", "bond_credit_grade", "VARCHAR")
    _add_column_if_absent(con, "bond", "bond_credit_rank", "INTEGER")

    # ── 1) 발행사 등급(기업신용등급): 대표 등급 + 평가사 갈림 여부 ──
    # PD_EVCO_CRD_GRD를 콤마 분리 → 각 등급 순위 조회
    #  · 대표: 순위 숫자가 가장 큰(=가장 낮은 등급) 값 선택 (보수적)
    #  · 갈림: 서로 다른 등급이 2개 이상이면 true
    con.execute("""
        WITH exploded AS (
            SELECT PD_NO,
                   trim(unnest(string_split(PD_EVCO_CRD_GRD, ','))) AS grade
            FROM bond
            WHERE PD_EVCO_CRD_GRD IS NOT NULL
        ),
        joined AS (
            SELECT e.PD_NO, e.grade, r.rk
            FROM exploded e
            LEFT JOIN grade_rank r ON e.grade = r.grade
            WHERE r.rk IS NOT NULL          -- 순위표에 없는 이상값 제외
        ),
        agg AS (
            SELECT PD_NO,
                   arg_max(grade, rk) AS worst_grade,     -- 가장 낮은 등급
                   COUNT(DISTINCT rk) > 1 AS is_split      -- 평가사 간 갈림
            FROM joined
            GROUP BY PD_NO
        )
        UPDATE bond
        SET issuer_credit_grade = agg.worst_grade,
            issuer_credit_split = agg.is_split
        FROM agg
        WHERE bond.PD_NO = agg.PD_NO
    """)

    # ── 2) 채권 등급(채권신용등급): CRD_GRD 표기 정규화 + 순위 ──
    # 끝의 '0' 하나만 제거 (AA0 → AA). +/- 는 유지. 원본 CRD_GRD는 안 건드림.
    con.execute("""
        UPDATE bond
        SET bond_credit_grade = regexp_replace(CRD_GRD, '0$', '')
        WHERE CRD_GRD IS NOT NULL
    """)
    # 정규화된 등급으로 순위 숫자 매핑
    con.execute("""
        UPDATE bond
        SET bond_credit_rank = grade_rank.rk
        FROM grade_rank
        WHERE bond.bond_credit_grade = grade_rank.grade
    """)

    # ── 검증 출력 ──
    n_issuer = con.execute(
        "SELECT COUNT(*) FROM bond WHERE issuer_credit_grade IS NOT NULL"
    ).fetchone()[0]
    n_split = con.execute(
        "SELECT COUNT(*) FROM bond WHERE issuer_credit_split"
    ).fetchone()[0]
    n_bond = con.execute(
        "SELECT COUNT(*) FROM bond WHERE bond_credit_grade IS NOT NULL"
    ).fetchone()[0]
    n_rank_null = con.execute(
        "SELECT COUNT(*) FROM bond WHERE bond_credit_grade IS NOT NULL AND bond_credit_rank IS NULL"
    ).fetchone()[0]
    print(f"[bond] 발행사 등급 {n_issuer}건 (평가사 갈림 {n_split}건)")
    print(f"[bond] 채권 등급 {n_bond}건 (순위 미매핑 {n_rank_null}건)")


def _add_column_if_absent(con, table, column, dtype):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")


if __name__ == "__main__":
    con = duckdb.connect(str(DB_PATH))
    apply_credit_grade(con)
    con.close()
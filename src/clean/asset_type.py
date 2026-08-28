"""자산군 정제 (fund 전용)

fund.or_attr_desc의 '06'은 파생상품형(파생형) 유형 코드인데 한글명이
누락되어 코드로만 남은 것이다. (금융감독원 펀드 유형 분류 + 데이터 검증으로 확인:
'06' 상품의 99%가 이름에 '파생' 포함, 나머지도 파생 활용 인덱스/TR 상품)

정제 단계에서 asset_type_std 컬럼에 명확한 값('파생형')으로 확정한다.
나머지 값은 원본을 그대로 표준 컬럼에 복사한다. 원본 or_attr_desc는 보존한다.

* 다른 테이블(etf_domestic '주식', etf_overseas 'Equity' 등)의 자산군은
  값이 이미 명확하므로 여기서 손대지 않고, 이후 개념 사전(매핑표)에서
  '주식 = 주식/Equity/주식형'처럼 용어 대응으로 처리한다.
"""
import duckdb
from src.config import DB_PATH

# fund.or_attr_desc 코드값 → 표준 자산군명
CODE_TO_STD = {
    "06": "파생형",
}


def apply_asset_type(con: duckdb.DuckDBPyConnection):
    """fund에 asset_type_std 컬럼을 추가한다.

    - or_attr_desc가 코드값(예: '06')이면 표준 자산군명으로 변환
    - 그 외에는 원본 값을 그대로 복사 (이미 명확한 한글명)
    """
    _add_column_if_absent(con, "fund", "asset_type_std", "VARCHAR")

    # 1) 기본: 원본 값을 그대로 복사
    con.execute("UPDATE fund SET asset_type_std = or_attr_desc")

    # 2) 코드값만 표준명으로 치환
    for code, std in CODE_TO_STD.items():
        con.execute(
            "UPDATE fund SET asset_type_std = ? WHERE or_attr_desc = ?",
            [std, code],
        )

    # 검증 출력
    n_code = con.execute(
        "SELECT COUNT(*) FROM fund WHERE or_attr_desc = '06'"
    ).fetchone()[0]
    n_std = con.execute(
        "SELECT COUNT(*) FROM fund WHERE asset_type_std = '파생형'"
    ).fetchone()[0]
    print(f"[fund] 자산군 정제: '06' {n_code}건 → 파생형 {n_std}건")
    # 남은 코드값(숫자로만 된 값)이 있는지 점검
    n_leftover = con.execute(r"""
        SELECT COUNT(*) FROM fund
        WHERE asset_type_std ~ '^[0-9]+$'
    """).fetchone()[0]
    print(f"[fund] 표준 컬럼에 남은 코드값: {n_leftover}건")


def _add_column_if_absent(con, table, column, dtype):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")


if __name__ == "__main__":
    con = duckdb.connect(str(DB_PATH))
    apply_asset_type(con)
    con.close()
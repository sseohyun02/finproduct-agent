"""전체 정제 파이프라인 실행

적재된 DuckDB에 모든 정제를 올바른 순서로 한 번에 적용한다.
개별 정제 파일을 따로 실행하는 대신 이 파일 하나로 전체를 재현할 수 있다.

실행: python -m src.clean.build_clean

정제 순서 (의존성 고려):
  1. risk_grade    위험등급 숫자화·유효성
  2. integrity     손상 행 플래그 (row_valid)
  3. credit_grade  신용등급 파싱 (bond)
  4. asset_type    자산군 06→파생형 (fund)
  5. fund_mgmt     운용사 표준화 (etf_domestic/overseas)
  6. normalize     날짜·통화·공백
"""
import duckdb
from src.config import DB_PATH

from src.clean.risk_grade import apply_risk_grade
from src.clean.integrity import apply_integrity
from src.clean.credit_grade import apply_credit_grade
from src.clean.asset_type import apply_asset_type
from src.clean.fund_mgmt import apply_fund_mgmt, apply_fund_mgmt_overseas
from src.clean.normalize import apply_normalize


def build_clean():
    con = duckdb.connect(str(DB_PATH))
    print("=" * 50)
    print("정제 파이프라인 시작")
    print("=" * 50)

    print("\n[1/6] 위험등급")
    apply_risk_grade(con)

    print("\n[2/6] 손상 행 무결성")
    apply_integrity(con)

    print("\n[3/6] 신용등급")
    apply_credit_grade(con)

    print("\n[4/6] 자산군")
    apply_asset_type(con)

    print("\n[5/6] 운용사")
    apply_fund_mgmt(con)          # 국내ETF
    apply_fund_mgmt_overseas(con) # 해외ETF

    print("\n[6/6] 날짜·통화·공백")
    apply_normalize(con)

    con.close()
    print("\n" + "=" * 50)
    print("정제 파이프라인 완료")
    print("=" * 50)


if __name__ == "__main__":
    build_clean()
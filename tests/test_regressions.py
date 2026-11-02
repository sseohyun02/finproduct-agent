import unittest
from pathlib import Path
import duckdb
from rdflib import Graph, Namespace, Literal
from rdflib.namespace import RDF
from src.agent.query_builder import QueryBuilder

ROOT = Path(__file__).resolve().parents[1]
F = Namespace('http://mirae-agent.example/ontology#')
E = Namespace('http://mirae-agent.example/data#')

class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.c = duckdb.connect(':memory:')
        self.c.execute('CREATE TABLE etf_domestic(pd_itm_no_ma VARCHAR, pd_nm VARCHAR, cu_fund_mgmt_co VARCHAR, cu_charge_rt DOUBLE, pd_risk_nm VARCHAR, du_last_aum DOUBLE, wu_inv_rgn VARCHAR, du_er_1y DOUBLE, row_valid BOOLEAN, pd_grp_no VARCHAR)')
        self.c.execute("INSERT INTO etf_domestic VALUES ('A0000Z0','ETF','운용사',0.1,'보통',100,'국내',10,true,'ETF'), ('A000001','ETN','발행사',0.2,'높음',100,'국내',20,true,'ETN')")
        self.g = Graph()
        self.q = QueryBuilder(ROOT/'data/dictionary/concepts.json', self.g, self.c)
    def tearDown(self):
        self.c.close()
    def test_return_sort_and_etn_exclusion(self):
        r = self.q.execute({'product_scope':'etf_domestic','sort':{'concept':'수익률','period':'1년','order':'desc'}})
        self.assertEqual(r['status'],'ok')
        self.assertEqual(r['data']['product_code'].tolist(),['0000Z0'])
    def test_codes_use_database_membership(self):
        self.assertEqual(self.q.filter_codes_by_scope({'0000Z0','000001'},'etf_domestic'),{'0000Z0'})
    def test_missing_holdings_is_not_empty_intersection(self):
        self.g.add((E.Theme_Test,RDF.type,F.Theme))
        self.g.add((E.Product_0000Z0,F.hasTheme,E.Theme_Test))
        self.g.add((E.Product_US,F.hasTheme,E.Theme_Test))
        r=self.q.execute({'product_scope':'etf_domestic','graph_conditions':[{'relation':'hasTheme','target':'Test'}],'aggregation':{'type':'intersection'}})
        self.assertEqual(r['status'],'data_missing')
        self.g.add((E.Product_0000Z0,F.holds,E.Company_A))
        r=self.q.execute({'product_scope':'etf_domestic','graph_conditions':[{'relation':'hasTheme','target':'Test'}],'aggregation':{'type':'intersection'}})
        self.assertEqual(r['returned_count'],1)
    def test_maturity_calendar_and_duration_ranges(self):
        self.c.execute('CREATE TABLE bond(pd_no VARCHAR,pd_nm VARCHAR,std_pd_mcls_nm VARCHAR,srfc_irt DOUBLE,risk_grade_num INTEGER,mat_dt VARCHAR,bond_credit_grade VARCHAR,corp_pretax_yield DOUBLE,row_valid BOOLEAN,info_base_dt VARCHAR,mat_dt_date DATE)')
        self.c.execute("INSERT INTO bond VALUES ('KR1','채권','회사채',3,3,'20271231','AA',3,true,'20260821','2027-12-31')")
        for concept,values in [('만기일',['2027-01-01','2027-12-31']),('만기',['1년','3년'])]:
            r=self.q.execute({'product_scope':'bond','sql_filters':[{'concept':concept,'operator':'between','value':values}]})
            self.assertEqual(r['status'],'ok',r)
            self.assertEqual(r['returned_count'],1)

    def test_legacy_maturity_dates_normalize_without_losing_rating(self):
        from src.agent.intent_validation import normalize_intent
        raw = {"product_scope":"bond", "sql_filters":[
            {"concept":"만기","operator":"between","value":["2027-01-01","2027-12-31"]},
            {"concept":"신용등급","operator":"gte","value":"AA-"}]}
        fixed = normalize_intent(raw)
        self.assertEqual(fixed["sql_filters"][0]["concept"], "만기일")
        self.assertEqual(fixed["sql_filters"][1], raw["sql_filters"][1])
        self.assertEqual(raw["sql_filters"][0]["concept"], "만기")

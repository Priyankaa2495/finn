"""Tests for the judgement calls that would silently mislead an investor if they broke."""
import os
import tempfile

os.environ["FINN_DATA_DIR"] = os.path.join(os.path.dirname(__file__), "..", "data")
os.environ["FINN_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["GEMINI_API_KEY"] = ""  # tests must pass with no model at all

from app import db, demo  # noqa: E402
from app.intel import classify, materiality, results  # noqa: E402
from app.network import graph  # noqa: E402
from app.network.resolve import norm_person, org_index, same_person  # noqa: E402
from app.research import guidance  # noqa: E402

db.init()


# ---- classification -----------------------------------------------------------
def test_rules_cover_common_exchange_subjects():
    assert classify.classify("Change in Management", "")[0] == "management_change"
    assert classify.classify("Credit Rating", "")[0] == "credit_rating"
    assert classify.classify("Trading Window-XBRL", "")[0] == "unclassified"
    assert classify.classify("Outcome of Board Meeting", "approved the unaudited financial results for the quarter ended")[0] == "results"
    assert classify.classify("Updates", "order win worth Rs 450 crore", use_llm=False)[0] == "press_release"


def test_results_intimation_is_not_results():
    s, tier, _ = materiality.score("results", "board meeting scheduled to consider results", 5)
    assert tier == "routine"


def test_cfo_exit_outranks_company_secretary_change():
    cfo = materiality.score("management_change", "", 5, mgmt={"role": "CFO", "leaving": True})[0]
    cs = materiality.score("management_change", "", 5, mgmt={"role": "Company Secretary", "leaving": True})[0]
    assert cfo - cs > 40


def test_lender_debt_issuance_is_routine():
    tier = materiality.score("fund_raise", "allotment of non-convertible debentures", 6, kind="bank")[1]
    assert tier == "routine"


# ---- entity resolution -----------------------------------------------------------
def test_org_aliases_and_suffixes():
    idx = org_index()
    assert idx.resolve("Havells India Ltd.")[0] == "havells"
    assert idx.resolve("Zomato Limited")[0] == "eternal"
    assert idx.resolve("Some Unknown Pvt Ltd")[0] is None


def test_name_only_match_is_never_certain():
    assert norm_person("Mr. Arvind Kulkarni, CA") == "arvind kulkarni"
    assert same_person("Rahul Sharma", "Rahul Sharma", {"tcs"}, {"infy"}) <= 0.5
    assert same_person("Rahul Sharma", "Mr. Rahul Sharma", {"infy"}, {"infy"}) >= 0.9


# ---- network ----------------------------------------------------------------------
ARVIND = {"name": "Arvind Kulkarni",
          "experience": [{"org": "Havells India", "role": "Head of Finance", "from": 2008, "to": 2019},
                         {"org": "Amber Enterprises", "role": "Chief Financial Officer", "from": 2019, "to": 2026}],
          "education": [{"school": "SRCC", "degree": "B.Com", "from": 2001, "to": 2004}]}


def test_same_function_colleague_beats_old_batchmate():
    paths = graph.paths_to(ARVIND)["paths"]
    assert paths[0]["via"]["name"] == "Neha Iyer"


def test_declared_relationship_is_used():
    target = {"name": "Suresh Venkataraman", "experience": [{"org": "Shriram Finance", "role": "ED", "from": 2014, "to": 2026}],
              "education": [{"school": "IIM Bangalore", "from": 2000, "to": 2002}]}
    assert graph.paths_to(target)["paths"][0]["via"]["name"] == "Anita Desai"


def test_current_insider_gets_compliance_caution():
    people = graph.who_to_call("deepakntr")
    insider = next(p for p in people if p["name"] == "Arjun Reddy")
    assert insider["caution"] and "UPSI" in insider["caution"]


# ---- research ---------------------------------------------------------------------
def test_guidance_parsing():
    assert guidance.parse_target("EBITDA margin of 18 to 20%") == (18.0, 20.0)
    assert guidance.parse_target("double-digit growth") == (10.0, None)
    assert guidance.horizon("for FY27 we expect", "Nov 2026") == 27
    assert guidance.call_fy("Nov 2026") == 27 and guidance.call_fy("May 2026") == 26


def test_analyst_questions_are_not_guidance():
    pages = ["Analyst: Is the 18 to 20% growth target for FY27 realistic? "
             "We expect FY27 revenue growth of 18 to 20% as capacity ramps up."]
    c = guidance.candidates(pages)
    assert len(c) == 1 and c[0]["text"].startswith("We expect")


def test_demochem_end_to_end_without_llm():
    demo.demochem()
    from app.research import engine
    r = engine.research("DEMOCHEM")
    g = r["guidance"]
    assert any(d["trend"] == "cut" for d in g["drift"])
    assert all(i["check"]["status"] in ("behind", "close", "judgement", "missed", "delivered", "on_track") for i in g["items"])
    assert r["cash"]["cum_conversion_5y"] < 70


def test_results_headline_math():
    d = {"quarters": {"periods": [f"Q{i}" for i in range(5)],
                      "rows": {"Sales": [100, 105, 110, 115, 120], "Operating Profit": [10, 11, 12, 13, 15],
                               "Profit before tax": [8, 9, 10, 11, 12], "Net Profit": [6, 7, 7.5, 8, 9]}}}
    h = results.headline("X", d)
    rev = h["metrics"][0]
    assert rev["qoq"] == round((120 - 115) / 115 * 100, 1) and rev["yoy"] == 20.0

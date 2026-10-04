import json
from unittest.mock import patch, MagicMock
from assistant.fast_valuation import direct_valuation
from assistant.suburb_brief import direct_comparison
from assistant.agent import ask

Q = "What is 8 Pohutukawa Parade, Riverhead worth?"
def evidence():
    return {"not_in_our_data":True,"corelogic":{"beds":4,"baths":2},"apex_estimate":{"available":True,"value":1680000,"suburb":"Riverhead"}}

def test_valuation_without_provider():
    with patch("assistant.agent.dispatch",return_value=json.dumps(evidence())) as dispatch, patch("assistant.fast_valuation.load_brief",return_value="### Around Riverhead\n80 days"):
        result=ask(None,Q)
    assert "$1,680,000" in result.text
    assert "Does this property have a swimming pool?" in result.text
    assert "80 days" in result.text
    dispatch.assert_called_once_with("find_address",{"address":"8 Pohutukawa Parade","suburb":"Riverhead"})

def test_no_lookup_for_complex_or_followup():
    dispatch=MagicMock()
    assert direct_valuation(Q,dispatch,[{"role":"user","content":"earlier"}]) is None
    assert direct_valuation(Q+" It has a pool.",dispatch) is None
    dispatch.assert_not_called()

def test_invalid_estimate_falls_back():
    for value in [None,0,-1,float("nan"),float("inf"),"1680000"]:
        data=evidence();data["apex_estimate"]["value"]=value
        assert direct_valuation(Q,lambda *args:json.dumps(data)) is None

def test_no_false_stats_claim():
    with patch("assistant.fast_valuation.load_brief",side_effect=RuntimeError("unavailable")):
        result=direct_valuation(Q,lambda *args:json.dumps(evidence()))
    assert result.tools_used==["find_address"]
    assert "temporarily unavailable" in result.text

def test_comparison_without_provider():
    session=MagicMock()
    with patch("db.SessionLocal",return_value=session), patch("ingest.sold_batch_ids",return_value=[1]), patch("assistant.suburb_brief.load_brief",side_effect=lambda name:"### Around "+name):
        result=ask(None,"Compare Riverhead and Kumeu")
    assert "### Around Riverhead" in result.text
    assert "### Around Kumeu" in result.text
    assert result.tools_used==["suburb_snapshot"]

def test_filtered_comparison_falls_back():
    for q in ["Compare Riverhead and Kumeu under $1m","Compare Riverhead and Kumeu: 4 bedroom houses"]:
        with patch("assistant.suburb_brief.load_brief") as loader:
            assert direct_comparison(q) is None
            loader.assert_not_called()

import json
from types import SimpleNamespace
import pytest
from assistant.record_report import record_report, money


def source():
    return {'id':42,'apex_url':'/property/42','address':'Example Home', 'land_m2':450,
        'pricing_comparison':{'asking_price':629000,'apex_value':723162}, 'comps_used':16}


def test_selected_action_does_not_call_paid_model_or_decrypt_keys(monkeypatch):
    from assistant import agent
    calls=[]
    def dispatch(name,args):
        calls.append((name,args))
        return json.dumps(source() if name=='get_property' else {'comparables':[]})
    def forbidden(*args,**kwargs):
        raise AssertionError('No generated investigation or key access')
    monkeypatch.setattr(agent,'dispatch',dispatch)
    monkeypatch.setattr(agent.providers,'run',forbidden)
    monkeypatch.setattr(agent.keys,'decrypt',forbidden)
    result=agent.ask(SimpleNamespace(), 'Investigate only Apex property ID 42, selected from latest shortlist', [agent.Turn('user','400m2 minimum')])
    assert calls==[('get_property',{'property_id':42}),('get_sold_comparables',{'property_id':42})]
    assert '$629,000' in result.text and '$723,162' in result.text and '$94,162' in result.text
    assert 'requirements have not been independently revalidated' in result.text
    assert result.iterations==0


def test_capped_sold_evidence_preserves_prices_missing_fields_and_sample_distinction():
    comps=[{'address':f'Home {i}', 'sale_price':650001.25,'sold_date':'2026-01-02'} for i in range(8)]
    out=record_report(42,lambda n,a:json.dumps(source() if n=='get_property' else {'comparables':comps})).text
    assert 'Showing 3 of 8' in out and 'Home 3' not in out
    assert '$650,001.25' in out and '| Valuation sample count | 16 |' in out
    assert 'Not recorded' in out and 'not necessarily the valuation sample' in out
    assert 'future optionality' not in out


@pytest.mark.parametrize('raw',['{}','[]','bad',json.dumps({'id':41,'apex_url':'/property/41'})])
def test_unverified_record_stops_report(raw):
    calls=[]
    def read(n,a):
        calls.append(n)
        return raw
    out=record_report(42,read).text
    assert "couldn't verify" in out and '/property/' not in out
    assert calls==['get_property']


def test_unavailable_sales_is_not_reported_as_zero_sales_and_imported_markdown_is_inert():
    r=source();r['address']='[Click](https://evil.test) | <script>\n# fake';r['deal_block_reason']='Conflicting prices'
    out=record_report(42,lambda n,a:json.dumps(r) if n=='get_property' else 'Database error').text
    assert '**Sold evidence unavailable:**' in out and 'No sold examples' not in out
    assert '[Click](' not in out and '<script>' not in out
    assert '**Record warning:** Conflicting prices' in out
    assert out.endswith('(/property/42)')


@pytest.mark.parametrize('value,expected',[(629000.0,'$629,000'),(10.01,'$10.01'),(10,'$10'),(None,'Not recorded'),(False,'Not recorded'),(float('nan'),'Not recorded')])
def test_money(value,expected):
    assert money(value)==expected


def test_progress_failures_do_not_break_report():
    def progress(*args): raise RuntimeError('callback unavailable')
    assert 'Example Home' in record_report(42,lambda n,a:json.dumps(source() if n=='get_property' else {'comparables':[]}),progress).text

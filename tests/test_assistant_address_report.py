import json
from types import SimpleNamespace
from assistant import agent
from assistant.address_report import PREFIX, address_report
from models import ImportBatch, PropertyForSale


def question(address='2/42 Synthetic Road', areas=None):
    return PREFIX + json.dumps({'address':address,'areas':areas or ['Glen Eden']}) + '. Find the exact address first.'


def add_home(s, batch, **kwargs):
    row=PropertyForSale(import_batch_id=batch.id,address=kwargs.pop('address','2/42 Synthetic Road'),suburb='Glen Eden',property_type='House',floor_area_m2=85,asking_price=629001,fair_value=723162,**kwargs)
    s.add(row);s.commit();return row


def batch(s,active=True):
    b=ImportBatch(batch_type='for_sale',filename='synthetic.csv',is_active=active,status='published')
    s.add(b);s.commit();return b


def test_exact_address_reports_without_model(db_session,monkeypatch):
    b=batch(db_session);home=add_home(db_session,b)
    add_home(db_session,b,address='42 Synthetic Road')
    def no_model(**kwargs): raise AssertionError('must not call model')
    monkeypatch.setattr(agent.providers,'run',no_model)
    result=agent.ask(SimpleNamespace(),question(' 2/42 SYNTHETIC ROAD '))
    assert f'/property/{home.id}' in result.text
    assert '$629,001' in result.text and '$723,162' in result.text
    assert 'most likely' not in result.text
    assert result.tools_used == ['get_property','get_sold_comparables']


def test_duplicates_require_choice(db_session):
    b=batch(db_session);one=add_home(db_session,b);two=add_home(db_session,b)
    def no_dispatch(*args): raise AssertionError('no ambiguous report')
    result=address_report(question(),no_dispatch)
    assert 'More than one' in result.text
    assert f'/property/{one.id}' in result.text and f'/property/{two.id}' in result.text


def test_inactive_held_and_nearby_are_not_substituted(db_session,monkeypatch):
    # An inactive / held / nearby row must never be presented AS the answer. It no
    # longer ends in "couldn't find", though: the exact address is looked up in the
    # external sources instead. With those mocked to hold nothing, we still get the
    # not-substituted guarantee — no /property/ link to a nearby or held row.
    monkeypatch.setattr('propertyvalue.pv_lookup_status',lambda q:(None,'not_found'))
    monkeypatch.setattr('external_estimates.homes_estimate',lambda q:None)
    old=batch(db_session,False);add_home(db_session,old)
    b=batch(db_session);add_home(db_session,b,is_held=True);add_home(db_session,b,address='42 Synthetic Road')
    result=address_report(question(),None)
    assert '/property/' not in result.text
    assert 'CoreLogic' in result.text or "hold a record" in result.text


def test_address_not_in_system_uses_external_sources(db_session,monkeypatch):
    # The whole point: an address that isn't a listing still returns real details
    # from CoreLogic / homes.co.nz, clearly as THEIR figures, with no /property/
    # link and no Ollie valuation substituted.
    fake={'canonical_address':'2/42 Synthetic Road, Glen Eden','property_type':'House',
          'beds':3,'baths':1,'floor_area_m2':110,'land_area_m2':400,
          'zoning':'Residential','cv':820000,'land_value':500000,
          'estimate_low':780000,'estimate_high':860000,'estimate_confidence':'HIGH',
          'last_sale_price':705000,'last_sale_date':'2019-03-01',
          'url':'https://www.propertyvalue.co.nz/x'}
    monkeypatch.setattr('propertyvalue.pv_lookup_status',lambda q:(fake,'ok'))
    monkeypatch.setattr('external_estimates.homes_estimate',lambda q:None)
    b=batch(db_session)  # active batch exists, but the address isn't in it
    result=address_report(question(),None)
    assert '/property/' not in result.text
    assert 'CoreLogic' in result.text
    assert '$820,000' in result.text
    assert 'not an Ollie valuation' in result.text


def test_invalid_input_cannot_fall_through_to_model():
    for q in [PREFIX+'oops',question(areas=['Glen Eden','Henderson']),PREFIX+'[]',PREFIX+'null']:
        assert 'one full street address' in address_report(q,None).text
    assert address_report('Tell me about Glen Eden',None) is None


def test_lookup_failure_is_not_no_matches(monkeypatch):
    from assistant import tools
    def fail():raise RuntimeError('private error')
    monkeypatch.setattr(tools,'SessionLocal',fail)
    result=address_report(question(),None)
    assert 'unavailable' in result.text and 'private error' not in result.text

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


def test_area_intelligence_backs_external_with_our_data(db_session):
    # The "wow" layer: an off-system address is backed by OUR read — what we think
    # it's worth and why (comp engine on real sales), plus the suburb's metrics.
    from assistant.address_report import _area_intelligence
    from models import ImportBatch, PropertySold, BatchType
    b = ImportBatch(batch_type=BatchType.SOLD.value, region="Auckland", filename="s.csv",
                    rows_total=0, is_active=True, status="published")
    db_session.add(b); db_session.flush()
    for i, p in enumerate([1750000, 1800000, 1825000, 1900000, 1680000,
                           1950000, 1775000, 1860000, 1720000, 1990000]):
        method = "auction" if i % 2 == 0 else "negotiation"
        cv = p * 0.90 if method == "auction" else p * 0.98
        db_session.add(PropertySold(
            import_batch_id=b.id, address=f"{10+i} Example Road", suburb="Remuera",
            property_type="House", beds=4, baths=2, floor_area_m2=200 + i,
            land_area_m2=210 + i, sale_price=p, cv_numeric=cv, sale_method=method,
            has_swimming_pool=(i < 5),   # 5 with a pool, 5 without -> pool effect line
            # Concentrate in 3 months so the monthly trend chart has >=3 per month.
            sold_date=f"2026-0{(i % 3)+1}-15", days_on_market=15 if method == "auction" else 40))
    db_session.commit()
    lines, meta = _area_intelligence("Remuera", "House", 4, 2, 202, 210)
    txt = "\n".join(lines)
    assert "worth about" in txt          # our valuation, with the "why"
    assert "sales on file" in txt        # suburb metrics
    assert "Recent sales" in txt
    assert "Best way to sell here" in txt and "auction" in txt.lower()  # method from metrics
    assert "Pool effect" in txt and "have a pool" in txt  # asks about a pool + area gap
    assert meta["ollie_value"] and meta["suburb_median"]  # numbers for the value chart
    assert "```apex-chart" in txt        # a graph, not just words
    import re as _re, json as _j
    _m = _re.search(r"```apex-chart\n(.*?)\n```", txt, _re.S)
    assert _m and _j.loads(_m.group(1))["type"] == "line"  # the monthly price trend


def test_lookup_failure_is_not_no_matches(monkeypatch):
    from assistant import tools
    def fail():raise RuntimeError('private error')
    monkeypatch.setattr(tools,'SessionLocal',fail)
    result=address_report(question(),None)
    assert 'unavailable' in result.text and 'private error' not in result.text

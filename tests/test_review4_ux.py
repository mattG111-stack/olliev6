"""Regression coverage for contradictions observed in the live UX review."""
import json
from dataclasses import asdict

from assistant.tools import get_property
from models import ImportBatch, PropertyForSale, User
from pricing.cashflow import CashflowAssumptions
from routers.properties import _filtered_query, get_for_sale, ForSaleRow
from routers.dashboards import today_brief, headline


def listing(db, batch, **overrides):
    fields = dict(import_batch_id=batch.id, address='39 Example Road', suburb='Henderson',
                  region='Auckland', property_type='House', floor_area_m2=100,
                  asking_price=900000, fair_value=1100000, margin=0.2, comps_used=8,
                  is_underpriced=True, can_subdivide=True, is_subdividable=True)
    fields.update(overrides)
    row = PropertyForSale(**fields)
    db.add(row)
    db.flush()
    return row


def batch(db, **overrides):
    fields=dict(batch_type='for_sale',filename='ux.csv',region='Auckland',is_active=True,status='published')
    fields.update(overrides)
    row=ImportBatch(**fields)
    db.add(row); db.flush()
    return row


def test_cashflow_tool_exposes_model_defaults_and_property_basis(db_session):
    b=batch(db_session)
    p=listing(db_session,b,buy_price=850000,est_weekly_rent=650)
    db_session.commit()
    result=json.loads(get_property(p.id))['cashflow_assumptions']
    defaults=asdict(CashflowAssumptions())
    for field in ('deposit_pct','mortgage_rate','loan_term_years','opex_pct'):
        assert result[field] == defaults[field]
    assert result['buy_price'] == 850000
    assert result['weekly_rent'] == 650
    assert result['purchase_price_basis'] == 'estimated buy price'


def test_related_records_preserve_conflicts_and_do_not_leak_held_or_other_units(db_session):
    b=batch(db_session)
    original=listing(db_session,b)
    duplicate=listing(db_session,b,address=' 39 EXAMPLE ROAD ',suburb='henderson ',asking_price=950000,floor_area_m2=80)
    listing(db_session,b,address='1/39 Example Road')
    listing(db_session,b,is_held=True)
    listing(db_session,b,suburb='Other suburb')
    listing(db_session,batch(db_session,is_active=False,status='staged'))
    db_session.commit()
    result=ForSaleRow.model_validate(get_for_sale(original.id,db_session))
    assert [r.id for r in result.related_listings] == [duplicate.id]
    assert result.related_listings[0].asking_price == 950000
    assert result.asking_price == 900000  # no silent price override
    assert db_session.query(PropertyForSale).count() == 6  # no destructive merge


def test_overview_counts_match_finders_with_quality_and_visibility_filters(db_session):
    b=batch(db_session)
    listing(db_session,b)
    listing(db_session,b,margin=0.06)
    listing(db_session,b,comps_used=1)
    listing(db_session,b,is_held=True)
    listing(db_session,b,can_subdivide=True,is_subdividable=False)
    listing(db_session,b,can_subdivide=False,is_subdividable=False)
    db_session.commit()
    expected_under=_filtered_query(db_session,b.id,underpriced=True,min_margin=0.075,min_comps=2).count()
    expected_sites=_filtered_query(db_session,b.id,subdividable=True).count()
    brief=today_brief(region='Auckland',db=db_session)
    head=headline(region='Auckland',me=User(role='admin'),db=db_session)
    assert brief.counts.underpriced == head.underpriced == expected_under
    assert brief.counts.subdividable == head.subdividable == expected_sites


def test_today_estimate_uses_valuation_not_buy_price(db_session):
    b=batch(db_session)
    p=listing(db_session,b,market_value=850000,fair_value=1100000,opportunity_score_pct=95)
    db_session.commit()
    result=today_brief(region='Auckland',db=db_session)
    signal=next(r for r in result.top_signals if r.id==p.id)
    assert signal.market_value == 1100000

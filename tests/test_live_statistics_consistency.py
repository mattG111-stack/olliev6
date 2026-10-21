"""Reproduce the live inconsistencies reported on 21 October."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock

from models import ImportBatch, PropertySold, PropertyForSale
from assistant.suburb_brief import load_snapshot, load_brief, direct_comparison
from assistant.tools import suburb_days_to_sell
from routers.properties import property_history
from routers.dashboards import today_brief


def batch(db, **kwargs):
    fields = dict(batch_type='for_sale', filename='synthetic.csv', region='Auckland',
                  status='published', is_active=True)
    fields.update(kwargs)
    b = ImportBatch(**fields); db.add(b); db.flush(); return b


def listing(db, b, **kwargs):
    fields = dict(import_batch_id=b.id, address='1 Test Road', suburb='Riverhead',
                  property_type='House', floor_area_m2=100, fair_value=900000,
                  asking_price=800000, margin=.125)
    fields.update(kwargs)
    p = PropertyForSale(**fields); db.add(p); db.flush(); return p


def test_timing_tool_and_panel_share_exact_deduplicated_period(db_session):
    db = db_session
    b = batch(db, batch_type='sold')
    today = datetime.now(ZoneInfo('Pacific/Auckland')).date()
    rows = [('1 Road', 10, 40, 900000, ' Riverhead '),
            ('1 Road', 10, 40, 900000, 'riverhead'),  # repeated import
            ('2 Road', 30, 80, 1000000, 'Riverhead'),
            ('3 Road', 200, 900, 900000, 'Riverhead'),  # outside panel period
            ('4 Road', 5, 1, 900000, 'Riverhead North'),  # wrong suburb
            ('5 Road', -1, 1, 900000, 'Riverhead'),  # future
            ('6 Road', 20, 1, 900000, 'Riverhead'),
            ('6 Road', 20, 1, 950000, 'Riverhead')]  # conflicting transaction
    for address, ago, days, price, suburb in rows:
        db.add(PropertySold(import_batch_id=b.id, address=address, suburb=suburb,
               sold_date=str(today-timedelta(days=ago)), days_on_market=days, sale_price=price))
    live = batch(db)
    listing(db, live)
    listing(db, live, is_held=True)
    listing(db, live, link_dead_at=datetime.now())
    private = batch(db, is_active=False, status='preview')
    listing(db, private)
    db.commit()
    snapshot = load_snapshot(' RIVERHEAD ')
    tool = json.loads(suburb_days_to_sell(' RIVERHEAD '))
    assert snapshot['count'] == 2
    assert snapshot['listings'] == tool['current_visible_listings'] == 1
    assert snapshot['average'] == tool['average_days_to_sell'] == 60
    assert tool['sales_with_a_listing_date'] == 2
    assert tool['period_start'] == str(snapshot['start'])
    assert tool['period_end'] == str(snapshot['end'])
    panel = load_brief('Riverhead')
    assert '60 days (2 sales)' in panel


def test_detailed_live_comparison_cannot_fall_back_to_conflicting_prose():
    question = 'Compare Glen Eden and Henderson using recent sales, selling time and current listings. Put the areas in side-by-side columns.'
    with patch('db.SessionLocal', return_value=MagicMock()), patch('publication_scope.approved_sold_batch_ids', return_value=[1]), patch('assistant.suburb_brief.load_brief', side_effect=lambda s: 'Verified '+s):
        answer = direct_comparison(question)
    assert answer.tools_used == ['suburb_snapshot']
    assert 'Verified Glen Eden' in answer.text and 'Verified Henderson' in answer.text
    assert '180-day' in answer.text


def test_history_uses_same_current_apex_value_without_leaking_drafts(db_session):
    db = db_session
    live = batch(db)
    p = listing(db, live, slug_id='exact-id', fair_value=900000, market_value=750000)
    staged = batch(db, status='preview', is_active=False)
    listing(db, staged, slug_id='exact-id', fair_value=9900000)
    db.commit()
    points = property_history(p.id, db).points
    assert len(points) == 1 and points[0].market_value == 900000
    p.fair_value = None; db.commit()
    assert property_history(p.id, db).points[0].market_value == 750000


def test_today_change_counts_visible_book_on_both_sides(db_session):
    db = db_session
    old = batch(db, is_active=False, status='archived')
    listing(db, old, slug_id='old')
    listing(db, old, is_held=True, slug_id='old-held')
    live = batch(db)
    listing(db, live, slug_id='new1')
    listing(db, live, slug_id='new2')
    listing(db, live, slug_id='dead', link_dead_at=datetime.now())
    listing(db, live, slug_id='sold', sold_seen_at=datetime.now())
    for i in range(4): listing(db, live, slug_id=f'held-{i}', is_held=True)
    db.commit()
    result = today_brief(region='Auckland', db=db)
    assert result.counts.total_for_sale == result.market_pulse.total_listings == 2
    assert result.market_pulse.listings_change == 1


def test_collection_status_does_not_claim_disabled_jobs_are_scheduled(monkeypatch):
    from portals.direct import settings, status
    for key in ('scraper_enabled', 'scraper_check_listings', 'portals_daily'):
        monkeypatch.setattr(settings, key, False)
    result = status()
    assert result['enabled'] is False and result['availability_enabled'] is False
    assert result['portal_daily_enabled'] is False
    assert 'disabled' in result['message']

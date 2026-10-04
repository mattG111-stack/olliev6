"""Deterministic suburb evidence attached to property answers."""
import json
import re
import math
from datetime import date, timedelta
from statistics import median
from zoneinfo import ZoneInfo
from datetime import datetime
from assistant.record_report import cell


def summarise(rows, today):
    # Same transaction imported twice is counted once. Conflicting prices are held.
    groups = {}
    for r in rows:
        try:
            day = date.fromisoformat(str(r.sold_date)[:10])
            price = float(r.sale_price)
        except (ValueError, TypeError):
            continue
        if day > today or not math.isfinite(price) or price <= 0 or not r.address:
            continue
        key = (' '.join(r.address.casefold().split()), day)
        groups.setdefault(key, []).append((price, r))
    sales = []
    for (_, day), values in groups.items():
        if len({v[0] for v in values}) != 1:
            continue
        sales.append((day, values[0][0], values[0][1]))
    start = today - timedelta(days=180)
    prior = start - timedelta(days=180)
    current = [r for r in sales if start < r[0] <= today]
    previous = [r for r in sales if prior < r[0] <= start]
    med = median([r[1] for r in current]) if current else None
    old = median([r[1] for r in previous]) if previous else None
    days = [float(r[2].days_on_market) for r in current
            if r[2].days_on_market is not None and 0 < float(r[2].days_on_market) <= 3650]
    return dict(start=start, end=today, count=len(current), median=med,
                average=math.floor(sum(days)/len(days)+0.5) if days else None,
                timing_count=len(days), prior_count=len(previous),
                change=(med/old-1)*100 if med and old and len(current)>=5 and len(previous)>=5 else None,
                recent=sorted(current, key=lambda r:r[0], reverse=True)[:5])


def render(suburb, data, listings):
    out=[f'### Around {cell(suburb)}',
         f"Recorded sales: {data['start']}–{data['end']} (180 days).",
         '', '| Suburb snapshot | Result |', '|---|---|',
         f"| Recorded sales | {data['count']} |",
         f"| Median sale price | {'$'+format(data['median'], ',.0f') if data['median'] else 'Unavailable'} |",
         f"| Average selling time | {str(data['average'])+' days ('+str(data['timing_count'])+' sales)' if data['average'] is not None else 'Unavailable — no listing durations recorded'} |",
         f"| Median price change vs preceding 180 days | {format(data['change'], '+.1f')+'%' if data['change'] is not None else 'Unavailable — fewer than 5 sales in either period'} |",
         f"| Current visible listings | {listings if listings is not None else 'Unavailable'} |",
         '', 'Price change describes the mix of recorded sales, not the change in value of an individual home.',
         '', '#### Recent recorded sales', '', '| Address | Sold | Price |', '|---|---|---|']
    for day, price, row in data['recent']:
        out.append(f'| {cell(row.address)} | {day} | ${price:,.0f} |')
    if not data['recent']:
        out.append('| No dated sales in this period | — | — |')
    out += ['', '```apex-map', json.dumps({'suburb':suburb}), '```']
    return '\n'.join(out)


def load_brief(suburb):
    from db import SessionLocal
    from models import PropertySold as S, PropertyForSale as P
    from ingest import sold_batch_ids
    from assistant.tools import _active
    from routers.properties import _hide_bad_data
    from sqlalchemy import func
    today = datetime.now(ZoneInfo('Pacific/Auckland')).date()
    with SessionLocal() as db:
        bids = sold_batch_ids(db, 'Auckland')
        rows = db.query(S.address,S.sold_date,S.sale_price,S.days_on_market).filter(
            S.import_batch_id.in_(bids),
            func.lower(func.trim(S.suburb)) == suburb.strip().lower()).all() if bids else []
        batch = _active(db, 'for_sale')
        listings = _hide_bad_data(db.query(P)).filter(P.import_batch_id==batch,
            func.lower(func.trim(P.suburb))==suburb.strip().lower()).count() if batch else None
    return render(suburb, summarise(rows,today),listings)


class SuburbBrief:
    def __init__(self, question=''):
        self.question = question
        self.suburb = None
        self.suburbs = []

    def wrap(self, dispatch):
        def call(name, arguments):
            result = dispatch(name, arguments)
            suburb = None
            if name in ('suburb_days_to_sell', 'value_property', 'search_listings'):
                suburb = arguments.get('suburb')
            if name in ('find_address', 'get_property'):
                try:
                    record = json.loads(result)
                    if isinstance(record, dict):
                        suburb = record.get('suburb')
                        if not suburb and record.get('not_in_our_data'):
                            suburb = arguments.get('suburb')
                except (ValueError, TypeError):
                    pass
            if isinstance(suburb, str) and suburb.strip():
                self.suburb = suburb.strip()[:120]
                if self.suburb.casefold() not in [s.casefold() for s in self.suburbs]:
                    self.suburbs.append(self.suburb)
            return result
        return call

    def append(self, text):
        if not self.suburb:
            return text
        blocks = []
        for suburb in (self.suburbs or [self.suburb])[:3]:
            try:
                blocks.append(load_brief(suburb))
            except Exception:
                blocks.append(f'### Around {cell(suburb)}\nSuburb statistics are temporarily unavailable.')
        block = '\n\n'.join(blocks)
        if '```apex-map' in block:
            text = re.sub(r'(?m)^[ \t]*```apex-map\b[^\n]*\n.*?^[ \t]*```[ \t]*(?:\n|$)', '', text, flags=re.DOTALL)
        if len(self.suburbs) > 1 and re.search(r'\bcompar(?:e|ison)\b', self.question, re.I):
            return '## Suburb comparison\n\nAll property types; the same 180-day period for each suburb.\n\n' + block
        return text.rstrip() + '\n\n' + block

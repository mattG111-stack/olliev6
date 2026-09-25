"""Conservative recent-listing window for verified newest-first searches.

Keep a seven-day overlap and force a full pass weekly. Sold dates are not
publication dates, so this optimisation must never be applied to sold searches.
"""
from datetime import datetime, timedelta, timezone, date
from urllib.parse import urlsplit, parse_qs


def start(state, source, kind, seeds, *, now=None):
    if state.get('pending_urls'):
        return
    now = now or datetime.now(timezone.utc)
    state['recent_old_pages'] = 0
    state['recent_cutoff'] = None
    if kind != 'for_sale' or len(seeds) != 1:
        return
    u = urlsplit(seeds[0])
    ordered = (source == 'oneroof' and '_order_latest-0_page_' in u.path) or (
        source == 'trademe' and parse_qs(u.query).get('sort_order') == ['expirydesc'])
    if not ordered:
        return
    try:
        full = datetime.fromisoformat(state['last_full_pass_at'])
        previous = datetime.fromisoformat(state['last_completed_pass_at'])
        if now - full >= timedelta(days=7) or previous > now:
            return
        state['recent_cutoff'] = (previous - timedelta(days=7)).date().isoformat()
    except (KeyError, TypeError, ValueError):
        return


def stop_after_page(state, rows):
    cutoff = state.get('recent_cutoff')
    if not cutoff or not rows:
        return False
    try:
        # Unknown dates/kinds cannot establish a safe boundary.
        old = all(row.get('kind') == 'for_sale' and
                  date.fromisoformat(str(row.get('listed_date'))) < date.fromisoformat(cutoff)
                  for row in rows)
    except (TypeError, ValueError):
        old = False
    state['recent_old_pages'] = state.get('recent_old_pages', 0) + 1 if old else 0
    return state['recent_old_pages'] >= 2

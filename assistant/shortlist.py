"""Bound listing evidence for explicitly sized sales shortlists per request."""
import json
import re

_WORDS = dict(zip(('one','two','three','four','five','six','seven','eight','nine','ten'), range(1,11)))

def requested_limit(question):
    match = re.search(r'\b(?:find|show|list|recommend|shortlist|give me)\s+(?:me\s+)?(?:up to\s+|exactly\s+|only\s+|the top\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?!bed\b|bedroom\b|bedrooms\b)(?:currently visible\s+)?(?:current\s+)?(?:properties|houses|homes|listings|options)\b', question, re.I)
    if not match:
        return None
    word = match.group(1).lower()
    value = _WORDS.get(word) if word in _WORDS else int(word)
    return value if 1 <= value <= 20 else None


def bounded_dispatch(dispatch, limit):
    """Bound each listing sample without inventing an exhaustive match count.

    Aggregate queries and sales-comparable evidence remain intact. This is a
    display boundary, not a mutation of data or a saved search preference.
    """
    def run(name, args):
        raw = dispatch(name, args)
        if name not in ('search_listings', 'query_data'):
            return raw
        if name == 'query_data' and 'properties_for_sale' not in str(args.get('sql', '')).lower():
            return raw
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return raw
        key = 'listings' if name == 'search_listings' else 'rows'
        rows = data.get(key) if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            return raw
        # A scalar COUNT/SUM is not a list of properties.
        if not all(isinstance(r, dict) and ('id' in r or 'address' in r) for r in rows):
            return raw
        # Each query is independent: a later query may refine the criteria.
        # A global identity budget hid valid candidates from those queries.
        admitted = set()
        kept = []
        for row in rows:
            identity = str(row['id']) if row.get('id') is not None else (str(row.get('address','')).casefold(),str(row.get('suburb','')).casefold())
            if identity in admitted or len(admitted) < limit:
                admitted.add(identity)
                kept.append(row)
        data[key] = kept
        # Preserve the underlying query's count. A display cap must never
        # rewrite evidence about how many rows the database returned.
        data['displayed_count'] = len(kept)
        data['query_rows_before_display_limit'] = len(rows)
        data['rows_omitted_from_display'] = len(rows) - len(kept)
        data['source_query_counts'] = {
            field: data.pop(field) for field in ('count', 'returned_count', 'row_count')
            if field in data
        }
        data['matching_total_verified'] = False
        data['shortlist_limit'] = limit
        data['shortlist_instruction'] = (
            'Choose at most the requested number across ALL tool results. '
            'This is a display sample, NOT an exhaustive match set. '
            'Never say these are the only matches or the complete set from these rows. '
            'query_rows_before_display_limit counts rows before this display cap; '
            'even that is not a verified total for the user brief. SQL LIMIT and '
            'tool filters may omit other matches. Say "Here are N options", '
            'not "Only N qualify". If the user needs a total, run a separate '
            'COUNT with every requested filter; do not count this sample.')
        if len(kept) < len(rows):
            data['shortlist_truncated'] = True
        return json.dumps(data)
    return run

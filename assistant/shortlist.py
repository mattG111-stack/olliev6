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
    """Do not hand the model extra listing identities to append in prose.

    Aggregate queries and sales-comparable evidence remain intact. This is a
    per-question boundary, not a mutation of data or a saved search preference.
    """
    admitted = set()
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
        kept = []
        for row in rows:
            identity = str(row['id']) if row.get('id') is not None else (str(row.get('address','')).casefold(),str(row.get('suburb','')).casefold())
            if identity in admitted or len(admitted) < limit:
                admitted.add(identity)
                kept.append(row)
        data[key] = kept
        if name == 'search_listings':
            data['count'] = data['returned_count'] = len(kept)
        else:
            data['row_count'] = len(kept)
        data['shortlist_limit'] = limit
        data['shortlist_instruction'] = ('These are the only properties to name in this answer. '
            'Returned count is not the total qualifying count. Do not claim only this many qualify '
            'unless a separate total_matches or COUNT proves it. Do not fetch extra identities.')
        if len(kept) < len(rows):
            data['shortlist_truncated'] = True
        return json.dumps(data)
    return run

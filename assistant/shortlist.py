"""Annotate shortlist evidence without discarding candidates before analysis."""
import json
import re

_WORDS = dict(zip(('one','two','three','four','five','six','seven','eight','nine','ten'), range(1,11)))

def requested_limit(question):
    match = re.search(r'\b(?:find|show|list|recommend|shortlist|give me)\s+(?:me\s+)?(?:up to\s+|exactly\s+|only\s+|the top\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?!bed\b|bedroom\b|bedrooms\b)(?:(?:currently visible|current visible|current)\s+)?(?:properties|houses|homes|listings|options)\b', question, re.I)
    if not match:
        return None
    word = match.group(1).lower()
    value = _WORDS.get(word) if word in _WORDS else int(word)
    return value if 1 <= value <= 20 else None


def bounded_dispatch(dispatch, limit):
    """Preserve tool evidence; the requested limit applies to the answer only.

    Underlying tools already have bounded query limits. Truncating them again
    here can hide candidates or conflicting duplicate records before analysis.
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
        data['evidence_rows_returned'] = len(rows)
        data['matching_total_verified'] = False
        data['shortlist_limit'] = limit
        data['shortlist_instruction'] = (
            'Choose at most the requested number across ALL tool results. '
            'These are candidate records for analysis, NOT an exhaustive match set. '
            'Never say these are the only matches or the complete set from these rows. '
            'evidence_rows_returned is not a verified total for the user brief. SQL LIMIT and '
            'tool filters may omit other matches. Say "Here are N options", '
            'not "Only N qualify". If the user needs a total, run a separate '
            'COUNT with every requested filter; do not count this sample.')
        return json.dumps(data)
    return run

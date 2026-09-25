"""Annotate shortlist evidence without discarding candidates before analysis."""
import json
import math
import html
import re

_WORDS = dict(zip(('one','two','three','four','five','six','seven','eight','nine','ten'), range(1,11)))

def requested_limit(question):
    question = re.sub(r'\b(find|show|list|recommend|shortlist|give me)\s+(?:me\s+)?my\s+', r'\1 ', question, flags=re.I)
    question = re.sub(r'\b(top|best)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b', r'\2', question, flags=re.I)
    question = re.sub(r'\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:best|top)\s+', r'\1 ', question, flags=re.I)
    match = re.search(r'\b(?:find|show|list|recommend|shortlist|give me)\s+(?:me\s+)?(?:up to\s+|exactly\s+|only\s+|the top\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?!bed\b|bedroom\b|bedrooms\b)(?:(?:currently visible|current visible|current)\s+)?(?:properties|houses|homes|listings|options)\b', question, re.I)
    if not match:
        return None
    word = match.group(1).lower()
    value = _WORDS.get(word) if word in _WORDS else int(word)
    return value if 1 <= value <= 20 else None


def conversation_limit(question, history):
    explicit = requested_limit(question)
    if explicit is not None:
        return explicit
    if re.search(r'\b(new search|start over|forget (?:that|the|my)|show all|all matches)\b', question, re.I):
        return None
    # Only inherit for recognisable continuations, never unrelated questions.
    if not re.search(r'\b(keep everything|keep my|same|remain|drop my budget|raise my budget|make it cheaper|filter tighter|refine|compare these)\b', question, re.I):
        return None
    for turn in reversed(history or []):
        if turn.role != 'user':
            continue
        if re.search(r'\b(new search|start over|forget (?:that|the|my)|show all|all matches)\b', turn.content, re.I):
            return None
        limit = requested_limit(turn.content)
        if limit is not None:
            return limit
    return None


def bounded_dispatch(dispatch, limit, evidence=None):
    """Preserve tool evidence; the requested limit applies to the answer only.

    Underlying tools already have bounded query limits. Truncating them again
    here can hide candidates or conflicting duplicate records before analysis.
    """
    def run(name, args):
        raw = dispatch(name, args)
        if name == 'get_property' and evidence is not None:
            try:
                record = json.loads(raw)
                if isinstance(record, dict) and record.get('id') and record.get('address'):
                    evidence.append(dict(record))
            except (ValueError, TypeError):
                pass
            return raw
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
        if evidence is not None:
            evidence.extend(dict(row) for row in rows)
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


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.replace('$', '').replace(',', '').strip()
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _field(row, *keys):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _price(row, estimate=False):
    comparison = row.get('pricing_comparison')
    if isinstance(comparison, dict):
        key = 'apex_value' if estimate else 'asking_price'
        if key in comparison:
            return _number(comparison[key])
    return _number(_field(row, *('fair_value', 'our_value') if estimate else ('asking_price', 'asking')))


def _text(value):
    # Tool strings are data, never Markdown structure, links or instructions.
    value = ' '.join(str(value or '').split())
    return html.escape(re.sub(r'([\\`*{}\[\]()#+!|_<>])', r'\\\1', value))


def _display(values, unit=''):
    known = {v for v in values if v is not None}
    if len(known) > 1:
        return 'Conflicting records'
    if not known:
        return 'Not recorded'
    value = next(iter(known))
    number = f'{value:,.2f}'.rstrip('0').rstrip('.')
    return f'${number}' if unit == '$' else f'{number}{unit}'


def render_shortlist(answer, evidence, limit, question='', previous_ids=None):
    """Bound recommendations at presentation, without truncating model evidence.

    Use the model's linked selection order (or address order if it supplied no
    links), but reconstruct facts from tool records. Free prose cannot add more
    properties or turn a sample into an exhaustive count. No extra model calls.
    Clarification/empty-search responses with no candidate selection are retained.
    """
    groups = {}
    ids = {}
    for row in evidence:
        address = row.get('address')
        if not isinstance(address, str) or not address.strip():
            continue
        identity = ' '.join(address.casefold().split())
        groups.setdefault(identity, []).append(row)
        rid = row.get('id')
        if isinstance(rid, int) and not isinstance(rid, bool) and rid > 0:
            ids[rid] = identity

    linked = [int(m.group(1)) for m in re.finditer(r'\]\(/property/(\d+)\)', answer)]
    selected = []
    for rid in linked:
        identity = ids.get(rid)
        if identity and identity not in [k for k, _ in selected]:
            selected.append((identity, rid))
    if not linked:
        mentioned = []
        for identity, rows in groups.items():
            match = re.search(r'(?<!\w)' + re.escape(rows[0]['address']) + r'(?!\w)', answer, re.I)
            if match:
                mentioned.append((match.start(), identity))
        for _, identity in sorted(mentioned):
            rid = next((rid for rid, key in ids.items() if key == identity), None)
            selected.append((identity, rid))
    if not selected:
        if linked:
            return 'I could not verify the property links for this shortlist. Please refine the area or budget so I can check again.'
        return answer

    selected = selected[:limit]
    lines = [f'Here {"is" if len(selected) == 1 else "are"} {len(selected)} option{"" if len(selected) == 1 else "s"} from the checked listing records.', '',
             '| Property | Asking price | Apex estimate | Beds / baths | Land | Floor |',
             '| --- | ---: | ---: | --- | ---: | ---: |']
    chart = []
    decision_rows = []
    for identity, rid in selected:
        rows = groups[identity]
        # Prefer the linked record's label; all fetched duplicate values remain
        # visible as conflicts instead of silently choosing the cheapest record.
        row = next((r for r in rows if r.get('id') == rid), rows[0])
        address = _text(row['address'])
        label = f'[{address}](/property/{rid})' if rid else address
        asking = _display([_price(r) for r in rows], '$')
        value = _display([_price(r, True) for r in rows], '$')
        beds = _display([_number(r.get('beds')) for r in rows])
        baths = _display([_number(r.get('baths')) for r in rows])
        land = _display([_number(_field(r, 'land_area_m2', 'land_m2')) for r in rows], ' m²')
        floor = _display([_number(_field(r, 'floor_area_m2', 'floor_m2')) for r in rows], ' m²')
        lines.append(f'| {label} | {asking} | {value} | {beds} / {baths} | {land} | {floor} |')
        prices = {_price(r) for r in rows} - {None}
        floors = {_number(_field(r, 'floor_area_m2', 'floor_m2')) for r in rows} - {None}
        if len(prices) == 1 and len(floors) == 1:
            decision_rows.append((address, next(iter(prices)), next(iter(floors))))
        if len(prices) == 1:
            chart.append({'label': row['address'], 'value': next(iter(prices))})
    if previous_ids:
        retained = sum(rid in previous_ids for _, rid in selected)
        lines += ['', f'Compared with the previous shortlist: {retained} retained and {len(selected) - retained} new in this selection.']
    if len(decision_rows) == len(selected) and len(decision_rows) > 1:
        cheapest = min(decision_rows, key=lambda r: r[1])
        largest = max(decision_rows, key=lambda r: r[2])
        if sum(r[1] == cheapest[1] for r in decision_rows) == 1:
            lines += ['', f'**If keeping the purchase price down is your priority:** investigate {cheapest[0]}, '
                      f'the lowest recorded asking price in this selection at {_display([cheapest[1]], "$")}. '
                      'Confirm condition and your must-haves before treating it as a suitable choice.']
        if cheapest[0] != largest[0] and largest[1] > cheapest[1] and largest[2] > cheapest[2]:
            lines += ['', '**The price-and-space trade-off:** '
                      f'{largest[0]} has {_display([largest[2] - cheapest[2]], " m²")} more recorded floor area than '
                      f'{cheapest[0]}, for {_display([largest[1] - cheapest[1]], "$")} more in asking price. '
                      'That compares recorded size and asking prices, not condition or value for money.']
    lines += ['', 'This is a shortlist, not a verified count of every matching property. Estimates are not guaranteed sale prices or profit. Conflicting records need checking before relying on a figure.']
    if re.search(r'\b(renovat\w*|condition|garage|garaging|outdoor|garden|yard)\b', question, re.I):
        lines += ['', 'Needs still to verify: car spaces do not confirm an enclosed garage; land area does not confirm usable outdoor space; build age does not establish condition or renovation requirements. These preferences need listing evidence or inspection before a match is confirmed.']
    if re.search(r'\b(chart|graph)\b', question, re.I) and chart:
        lines += ['', '```apex-chart', json.dumps({'type': 'bar', 'title': 'Shortlisted asking prices', 'source': 'Checked listing records for this shortlist; missing or conflicting prices omitted.', 'unit': 'NZD', 'data': chart}), '```']
    lines += ['', 'Which option would you like to investigate, or which filter should we refine?']
    return '\n'.join(lines)

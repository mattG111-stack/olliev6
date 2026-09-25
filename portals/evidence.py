"""Carry source estimates and disclosed transaction history without pricing leakage."""
import json
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo
from portals.page_data import number, date as source_date
from portals import ESTIMATE_COLUMNS


def positive(value):
    value=number(value)
    return value if value is not None and math.isfinite(value) and value>0 else None


def history(rows):
    entries={}
    for row in rows:
        for sale in row.get('sale_history') or []:
            if not isinstance(sale,dict):continue
            if sale.get('type') not in (None,'sale'):continue
            when=source_date(sale.get('saleDate') or sale.get('labelTime'))
            try:
                if date.fromisoformat(str(when))>datetime.now(ZoneInfo('Pacific/Auckland')).date():continue
            except (ValueError,TypeError):continue
            price=positive(sale.get('salePrice') if 'salePrice' in sale else sale.get('labelValue'))
            key=(when,price)
            target=entries.setdefault(key,{'saleDate':when,'salePrice':price,'sources':[]})
            origins=sale.get('sources') or [{'source':row.get('source'),'url':row.get('url')}]
            for origin in origins:
                if origin not in target['sources']:target['sources'].append(origin)
    return sorted(entries.values(),key=lambda x:x['saleDate'],reverse=True)


def apply_to_property(prop, raw_json):
    try:data=json.loads(raw_json or '{}')
    except (ValueError,TypeError):return
    if not isinstance(data,dict) or not data.get('_apex_direct'):return
    if data.get('land_slope_contour') and not prop.land_slope_contour:
        prop.land_slope_contour=str(data['land_slope_contour'])
    if data.get('building_age_decade') and not prop.building_age:
        decade=str(data['building_age_decade']).rstrip('s')
        if len(decade)==4 and decade.isdigit():prop.building_age=decade+'s'
    for source in ('oneroof','homes','trademe','realestate'):
        prefix=source+'_estimate'
        value=positive(data.get(prefix))
        if value is None:continue
        mid,low,high,url_col=ESTIMATE_COLUMNS[source]
        setattr(prop,mid,value)
        for suffix,column in [('_low',low),('_high',high)]:
            bound=positive(data.get(prefix+suffix))
            if bound is not None:setattr(prop,column,bound)
        origins=(data.get('provenance') or {}).get(prefix) or []
        if isinstance(origins,dict):origins=[origins]
        origin=next((o for o in origins if o.get('source')==source),None)
        if url_col and origin and origin.get('url'):setattr(prop,url_col,origin['url'])
    sales=history([data])
    disputed={s['saleDate'] for s in sales if len({x['salePrice'] for x in sales
              if x['saleDate']==s['saleDate'] and x['salePrice'] is not None})>1}
    # Source provenance stays in approved review evidence; the customer-facing
    # timeline contains only unambiguous dates and disclosed prices.
    if sales:prop.sale_history_json=json.dumps([
        {'saleDate':s['saleDate'],'salePrice':s['salePrice']} for s in sales
        if s['saleDate'] not in disputed],ensure_ascii=False)
    # Keep source assertions in the timeline. Only an unambiguous disclosed
    # transaction may fill the single latest-sale summary.
    disclosed=[s for s in sales if s['salePrice'] is not None]
    if disclosed:
        latest=disclosed[0]['saleDate']
        prices={s['salePrice'] for s in disclosed if s['saleDate']==latest}
        if len(prices)==1 and not prop.valuation_last_sold_value:
            prop.valuation_last_sold_value=next(iter(prices))
            prop.valuation_last_sold_date=latest

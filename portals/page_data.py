#!/usr/bin/env python3
"""Apex Auckland public property collector. Python 3.9+, no dependencies."""
from __future__ import annotations
import copy, html, json, re, unicodedata, math
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

VERSION = '1.1.1'
SITES = ('oneroof', 'realestate', 'trademe', 'homes')
KINDS = ('for_sale', 'rent', 'sold')

def now(): return datetime.now(timezone.utc).isoformat()
def dumps(v): return json.dumps(v, ensure_ascii=False, separators=(',', ':'))
def present(v): return v is not None and v != '' and v != [] and v != {} and not (isinstance(v,str) and re.fullmatch(r'\$[0-9a-f]+',v))
def pick(d,*keys): return next((d[k] for k in keys if present(d.get(k))),None)
def number(v):
    if isinstance(v,bool) or v is None:return None
    if isinstance(v,(int,float)):return v if math.isfinite(v) else None
    s=html.unescape(str(v)).replace(',','').strip()
    m=re.fullmatch(r'\$?\s*(-?\d+(?:\.\d+)?)\s*([kKmM])?(?:\s*(?:m²|m2|sqm|ha))?',s)
    if not m:return None
    n=float(m[1]);return n * (10000 if s.lower().endswith('ha') else 1000 if m[2] in ('k','K') else 1000000 if m[2] in ('m','M') else 1)
def area(v,unit):
    n=number(v)
    return n*10000 if n is not None and str(unit).lower() in ('ha','hectares','hectare') else n

def date(v):
    if not present(v):return None
    try:
        if str(v).isdigit() and len(str(v))>=10:
            from zoneinfo import ZoneInfo
            return datetime.fromtimestamp(float(v),ZoneInfo('Pacific/Auckland')).date().isoformat()
    except (ValueError,OverflowError):return None
    return str(v)[:10]

class Page(HTMLParser):
    def __init__(self,text):
        super().__init__();self.scripts=[];self.links=[];self.attrs=None;self.buf='';self.feed(text)
    def handle_starttag(self,t,a):
        d=dict(a)
        if t=='script':self.attrs=d;self.buf=''
        if t=='a':self.links.append(d.get('href',''))
    def handle_data(self,s):
        if self.attrs is not None:self.buf+=s
    def handle_endtag(self,t):
        if t=='script' and self.attrs is not None:self.scripts.append((self.attrs,self.buf));self.attrs=None

def payloads(text):
    """Decode JSON and React Flight data only. Never execute site JavaScript."""
    out=[];flight=''
    for attrs,s in Page(text).scripts:
        if attrs.get('type') in ('application/ld+json','application/json','fastboot/shoebox') and attrs.get('id') not in ('shoebox-anon','shoebox-device','shoebox-ff'):
            try:out.append(json.loads(s))
            except ValueError:pass
        # JSON decoder understands escaped brackets/quotes in script payloads.
        for match in re.finditer(r'self\.__next_f\.push\(',s):
            try:
                a,_=json.JSONDecoder().raw_decode(s[match.end():])
                if len(a)>1 and isinstance(a[1],str):flight+=a[1]
            except (ValueError,TypeError):pass
    raw=flight.encode();records={};pos=0
    while pos<len(raw):
        m=re.match(rb'([0-9a-f]+):',raw[pos:])
        if not m:
            end=raw.find(b'\n',pos);pos=end+1 if end>=0 else len(raw);continue
        key=m[1].decode();pos+=m.end()
        t=re.match(rb'T([0-9a-f]+),',raw[pos:])
        if t:
            pos+=t.end();length=int(t[1],16);records[key]=raw[pos:pos+length].decode('utf-8');pos+=length
        else:
            end=raw.find(b'\n',pos);end=len(raw) if end<0 else end
            try:records[key]=json.loads(raw[pos:end])
            except ValueError:pass
            pos=end+1
    def resolve(x,seen):
        if isinstance(x,str):
            if x.startswith('$$'):return x[1:]
            if x.startswith('$') and x[1:] in records and x[1:] not in seen:return resolve(records[x[1:]],seen|{x[1:]})
        if isinstance(x,dict):return {k:resolve(v,seen) for k,v in x.items()}
        if isinstance(x,list):return [resolve(v,seen) for v in x]
        return x
    out.extend(resolve(v,{k}) for k,v in records.items())
    return out

def walk(x):
    if isinstance(x,dict):
        yield x
        for v in x.values():yield from walk(v)
    elif isinstance(x,list):
        for v in x:yield from walk(v)
    elif isinstance(x,str) and x[:1] in ('{','['):
        try:yield from walk(json.loads(x))
        except ValueError:pass

def extract(text,site):
    objects=list(walk(payloads(text)));found={}
    entities={(d.get('type'),str(d.get('id'))):d for d in objects if 'attributes' in d}
    for d in objects:
        if site=='trademe' and isinstance(d.get('propertyDetails'),dict) and d.get('propertyId') and d.get('state')==1:
            path=d.get('url','')
            if not path.startswith('/property/insights/profile/'):continue
            def snake(value):
                if isinstance(value,dict):return {re.sub(r'(?<!^)(?=[A-Z])','_',k).lower():snake(v) for k,v in value.items()}
                if isinstance(value,list):return [snake(v) for v in value]
                return value
            url='https://www.trademe.co.nz/a'+path
            record={'_apex_homes':snake(d),'_apex_kind':'sold','_original_trademe':d}
        elif site=='trademe' and d.get('listingId') and str(d.get('canonicalPath','')).startswith('/property/residential/'):
            path=d['canonicalPath']
            if not re.fullmatch(r'/property/residential/(sale|rent)/[^?#]+/listing/\d+',path):continue
            url='https://www.trademe.co.nz/a'+path
            record={'_apex_trademe':d,'_apex_kind':'rent' if '/rent/' in path else 'for_sale'}
        elif site=='homes' and isinstance(d.get('property_details'),dict) and d.get('property_id') and d.get('state') in (0,1):
            path=d.get('url','')
            if not re.fullmatch(r'/[^?#]+/[^/]+/[^/]+/[^/]+',path):continue
            url='https://homes.co.nz/address'+path
            record={'_apex_homes':d,'_apex_kind':'sold' if d['state']==1 else 'for_sale'}
        elif site=='oneroof':
            if 'street' not in d or not str(d.get('shareLink','')).startswith('https://www.oneroof.co.nz/property/'):continue
            url=d['shareLink'];record=d
        elif d.get('type')=='listing' and 'attributes' in d:
            a=d['attributes'];url=a.get('website-full-url')
            if not url:continue
            record=copy.deepcopy(d)
            record['related']={k:[entities.get((r['type'],str(r['id'])),r) for r in v.get('data',[])] for k,v in d.get('relationships',{}).items() if isinstance(v.get('data'),list)}
        elif d.get('__typename')=='Property' and d.get('websiteFullUrl'):
            url=d['websiteFullUrl'];record=d
        else:continue
        if url not in found or len(dumps(record))>len(dumps(found[url])):found[url]=record
    # Structured public listing metadata. Trade Me adapter intentionally accepts
    # explicit listing/property entities only, never arbitrary price-bearing JSON.
    for d in objects:
        if d.get('@type') not in ('RealEstateListing', 'House', 'Apartment', 'SingleFamilyResidence', 'Residence'):
            continue
        url=d.get('url')
        obj=d.get('about',d)
        if not isinstance(obj,dict) or not isinstance(obj.get('address'),dict) or not isinstance(url,str):continue
        if url not in found:found[url]={'_apex_schema':d}
    if site=='oneroof':
        for container in objects:
            data=container.get('data')
            if isinstance(data,dict) and data.get('shareLink') in found and 'histories' in container:
                found[data['shareLink']]['_property_history']=container['histories']
                found[data['shareLink']]['_value_over_time']=container.get('overTime')
    return found

def normalise(site,kind,url,raw,collected=None):
    n={'source':site,'listing_kind':kind,'url':url,'scraped_at':collected or now()}
    if '_apex_trademe' in raw or '_apex_homes' in raw:
        from portals.public_adapters import normalise_public
        return normalise_public(n,raw)
    if '_apex_schema' in raw:
        return schema_normalise(site,kind,url,raw['_apex_schema'],collected)
    if site=='oneroof':
        a=raw;pub=a.get('publicRecords') or {};pub=pub if isinstance(pub,dict) else {};avm=a.get('avm') or {};avm=avm if isinstance(avm,dict) else {}
        n.update(address=a.get('street'),suburb=a.get('suburb'),district=a.get('district'),region=a.get('region'),listing_id=pick(a,'houseId','id'),property_id=a.get('propertyId'),property_type=a.get('category'),latitude=number(a.get('lat')),longitude=number(a.get('lng')))
        for dest,keys in {'key_bedrooms':('bedrooms',),'key_bathrooms':('bathrooms',),'key_carspaces':('carspaces',),'cv_numeric':('rv',),'year_built':('buildingAge',)}.items():n[dest]=number(pick(a,*keys))
        n['key_floor_area']=area(pick(a,'floorArea','floorarea'),a.get('floorAreaUnit'))
        n['key_land_area']=area(pick(a,'landArea','landarea'),a.get('landAreaUnit'))
        # Text includes explicit units; numeric unit codes are not guessed.
        for dest,key in [('key_floor_area','floorAreaString'),('key_land_area','landAreaString')]:
            s=html.unescape(str(a.get(key) or ''))
            if s:
                amount=re.match(r'([\d,.]+)\s*(ha|m²|m2)',s)
                if amount:n[dest]=area(amount[1],amount[2])
        n.update(price_display=' '.join(str(a[k]) for k in ('priceBold','priceLight') if present(a.get(k))),description=a.get('description'),title=a.get('teaser'),images=a.get('images'),floorplans=a.get('floorPlanImages'),agents=a.get('agents'),agency=a.get('office'),open_homes=a.get('openHomes'),videos=a.get('video'),virtual_tours=a.get('threeDview'),listed_date=date(a.get('marketTime')),available_date=date(a.get('availableDate')),land_value_numeric=number(pub.get('landValue')),improvement_value_numeric=number(pub.get('improvementValue')),cv_date=date(pub.get('valuationAt')),oneroof_estimate=number(avm.get('avm')),oneroof_estimate_low=number(avm.get('low')),oneroof_estimate_high=number(avm.get('high')))
        for item in (pub.get('data') or []):
            if item.get('alias') in ('ld','ed','unitary','cond','council'):
                n[{'ld':'legal_description','ed':'estate_description','unitary':'zoning','cond':'condition','council':'council'}[item['alias']]]=item.get('value')
        sold=a.get('soldInfo') or {}
        if kind=='sold':n.update(sale_price=number(sold.get('soldPrice')),sold_date=date(sold.get('soldDate')))
        # Search ranking prices are NOT advertised asking prices.
        display=str(a.get('priceBold') or '')
        if re.fullmatch(r'(?i)(?:asking(?: price)?\s*)?\$[\d,.]+(?:[km])?',display):
            val=number(re.sub(r'(?i)^asking(?: price)?\s*','',display))
            if kind=='for_sale':n['price_numeric']=val
            if kind=='rent' and 'week' in str(a.get('priceLight','')).lower():n['rent_weekly']=val
    else:
        a=raw.get('attributes',raw)
        def g(*ks):return pick(a,*ks)
        addr=a.get('address') or {};full=pick(addr,'full-address','fullAddress')
        n.update(address=full.split(',')[0].strip() if full else pick(addr,'displayStreetFromAddress','street'),suburb=addr.get('suburb'),district=addr.get('district'),region=addr.get('region'),listing_id=raw.get('id'),property_id=g('property-short-id','shortId'),property_type=g('listing-sub-type','listingSubType'),latitude=number(pick(addr,'latitude')),longitude=number(pick(addr,'longitude')))
        for dest,keys in {'key_bedrooms':('bedroom-count','bedroomsTotalCount','bedroomCount'),'key_bathrooms':('bathrooms-total-count','bathroomsTotalCount','bathroom-count'),'year_built':('year-built','yearBuilt')}.items():n[dest]=number(g(*keys))
        parking=[number(g(*ks)) for ks in [('parking-garage-count','parkingGarageCount'),('parking-covered-count','parkingCoveredCount'),('parking-other-count','parkingOtherCount')]]
        n['key_carspaces']=sum(v for v in parking if v is not None) if any(v is not None for v in parking) else None
        n['key_floor_area']=area(g('floor-area','floorArea'),g('floor-area-unit','floorAreaUnit'));n['key_land_area']=area(g('land-area','landArea'),g('land-area-unit','landAreaUnit'))
        n.update(title=g('header'),description=g('description'),price_display=g('price-display','priceDisplay'),floorplans=g('floorplans'),open_homes=g('open-homes','openHomes'),videos=g('videos'),virtual_tours=g('virtual-walkthroughs','virtualWalkthroughs'),listed_date=date(g('published-date','createdDate')),available_date=date(g('available-from-date','availableFromDate')),type_of_title=g('title-type','titleType'),schools=g('schools'),features=g('features'),sale_history=g('salesHistory'),agents=raw.get('related',{}).get('agents',g('agents')),agency=raw.get('related',{}).get('offices',g('offices')))
        n['images']=[]
        for p in a.get('photos') or []:
            if isinstance(p,str):n['images'].append(p)
            elif isinstance(p,dict):
                base=pick(p,'base-url','baseUrl');suffix=pick(p,'large','medium')
                if base:n['images'].append(('https://mediaserver.realestate.co.nz' if base.startswith('/') else '')+base+(suffix or ''))
        councils=a.get('councilEvaluations') or []
        if councils:
            c=max(councils,key=lambda c:c.get('currentValuationDate') or '')
            n.update(cv_numeric=number(c.get('capitalValue')),cv_date=c.get('currentValuationDate'),land_value_numeric=number(c.get('landValue')),improvement_value_numeric=number(c.get('improvementsValue')))
        estimates=a.get('estimatedValues') or []
        if estimates:
            e=max(estimates,key=lambda e:e.get('estimatedDate') or '')
            n.update(realestate_estimate=number(e.get('valueMid')),realestate_estimate_low=number(e.get('valueLow')),realestate_estimate_high=number(e.get('valueHigh')),realestate_estimate_date=e.get('estimatedDate'))
        if kind=='sold':
            # Historical sale stays dated; never substitute a current listing price.
            sales=[s for s in (a.get('salesHistory') or []) if s.get('saleDate')]
            if sales:
                s=max(sales,key=lambda s:s['saleDate']);n.update(sale_price=number(s.get('salePrice')),sold_date=s['saleDate'],sale_record_basis='latest_disclosed_historical_sale')
        display=str(n.get('price_display') or '')
        if kind=='for_sale' and re.fullmatch(r'\$[\d,.]+(?:[kmKM])?',display):n['price_numeric']=number(display)
        if kind=='rent' and re.fullmatch(r'\$[\d,.]+ per week',display):n['rent_weekly']=number(display.removesuffix(' per week'))
    if site=='oneroof' and raw.get('_property_history'):
        n['sale_history']=raw['_property_history']
    if site=='realestate' and kind=='sold' and raw.get('_search_record'):
        summary=normalise(site,kind,url,raw['_search_record'],collected)
        for key,value in summary.items():
            if key in ('sale_price','sold_date','sale_history','realestate_estimate','realestate_estimate_low','realestate_estimate_high','realestate_estimate_date','cv_numeric','cv_date','land_value_numeric','improvement_value_numeric') or not present(n.get(key)):n[key]=value
    if site=='oneroof':
        public=raw.get('publicRecords') or {}
        facts={x.get('alias'):x.get('value') for x in (public.get('data') or []) if isinstance(x,dict)} if isinstance(public,dict) else {}
        aliases={'timeOnMarket':'key_time_on_market','wwc':'water_capacity','tt':'type_of_title','title':'title_reference','cont':'land_slope_contour','age':'building_age','cond':'building_condition','view':'view_type'}
        for alias,field in aliases.items():
            if present(facts.get(alias)):n[field]=html.unescape(str(facts[alias]))
        n['last_updated']=date(raw.get('updateAt'))
        for key,field in [('rateableValueChange','valuation_rateable_change_pct'),('landValueChange','valuation_land_change_pct'),('improvementValueChange','valuation_improvement_change_pct')]:n[field]=number(public.get(key))
        n['council_valuation_summary']=' | '.join(f'{k}: {v}' for k,v in public.items() if k in ('landValue','improvementValue','rateableValue','valuationAt'))
        sales=[x for x in (raw.get('_property_history') or []) if x.get('type')=='sale' and number(x.get('labelValue')) is not None]
        if sales:
            latest=max(sales,key=lambda x:int(x.get('labelTime') or 0))
            n['valuation_last_sold_value']=latest.get('labelValue');n['valuation_last_sold_date']=date(latest.get('labelTime'))
    else:
        a=raw.get('attributes',raw)
        for field,keys in {'last_updated':('updated-date','updatedDate'),'rates_annual':('rates',),'type_of_title':('title-type','titleType')}.items():
            v=pick(a,*keys)
            if present(v):n[field]=date(v) if field=='last_updated' else v
        history=a.get('salesHistory') or (raw.get('_search_record') or {}).get('salesHistory') or []
        sales=[x for x in history if x.get('saleDate') and number(x.get('salePrice')) is not None]
        if sales:
            latest=max(sales,key=lambda x:x['saleDate']);n['valuation_last_sold_value']=latest['salePrice'];n['valuation_last_sold_date']=latest['saleDate']
    for k in ('key_floor_area','key_land_area','cv_numeric','price_numeric','rent_weekly','sale_price'):
        if n.get(k)==0:n[k]=None
    return {k:v for k,v in n.items() if present(v)}

ABBREVIATIONS={'rd':'road','st':'street','ave':'avenue','dr':'drive','pl':'place','cres':'crescent','ln':'lane','tce':'terrace','mt':'mount','hwy':'highway'}
def text_key(s):
    s=''.join(c for c in unicodedata.normalize('NFKD',str(s).lower()) if not unicodedata.combining(c))
    s=re.sub(r'[^a-z0-9/ &-]+',' ',s)
    return ' '.join(ABBREVIATIONS.get(t,t) for t in s.split())
def match_key(n):
    address=text_key(n.get('address',''));suburb=text_key(n.get('suburb',''));district=text_key(n.get('district',''))
    # Conservative exact address matching; ranges/multiple properties need review.
    if not re.match(r'^\d',address) or any(x in address for x in ('&','-')) or not suburb or not district:return None
    return address,suburb,district,text_key(n.get('region',''))

PROPERTY_FIELDS={'key_bedrooms','key_bathrooms','key_carspaces','key_floor_area','key_land_area','property_type','year_built','type_of_title','legal_description','estate_description','zoning','condition','council','latitude','longitude'}
def merge(records):
    groups={}
    for record in records:
        key=match_key(record) or ('unmatched',record['source'],record['url'])
        groups.setdefault(key,[]).append(record)
    out=[]
    for group in groups.values():
        for kind in KINDS:
            same=[r for r in group if r['listing_kind']==kind]
            if not same:continue
            # Keep sold transactions separate when dates differ.
            dates=set(r.get('sold_date') for r in same) if kind=='sold' else {None}
            for sale_date in dates:
                own=[r for r in same if kind!='sold' or r.get('sold_date')==sale_date]
                own.sort(key=lambda r:r['scraped_at'],reverse=True)
                row={};sources={};conflicts={}
                for r in own:
                    for field,value in r.items():
                        if field in ('source','url','scraped_at','listing_id','property_id'):continue
                        source={'source':r['source'],'url':r['url'],'scraped_at':r['scraped_at']}
                        if not present(row.get(field)):row[field]=value;sources[field]=[source]
                        elif row[field]==value:sources[field].append(source)
                        else:conflicts.setdefault(field,[]).append({'value':value,**source})
                # Physical facts may fill across categories only from another site.
                own_sites={r['source'] for r in own}
                for r in sorted(group,key=lambda r:r['scraped_at'],reverse=True):
                    if r['source'] in own_sites or r['listing_kind']==kind:continue
                    for field in PROPERTY_FIELDS:
                        if not present(row.get(field)) and present(r.get(field)):
                            row[field]=r[field];sources[field]=[{'source':r['source'],'url':r['url'],'scraped_at':r['scraped_at'],'from_listing_kind':r['listing_kind']}]
                row.update(url=own[0]['url'],source_urls=list(dict.fromkeys(r['url'] for r in own)),provenance=sources,conflicts=conflicts,needs_review=bool(conflicts))
                out.append(row)
    return out

def search_url(site,kind,page):
    if site=='oneroof':return f"https://www.oneroof.co.nz/search/{ {'for_sale':'houses-for-sale','rent':'houses-for-rent','sold':'sold'}[kind]}/region_auckland-35_page_{page}"
    return f"https://www.realestate.co.nz/residential/{ {'for_sale':'sale','rent':'rental','sold':'sold'}[kind]}/auckland?page={page}"


def schema_normalise(site,kind,url,data,collected=None):
    obj=data.get('about',data);a=obj['address'];geo=obj.get('geo') or {}
    row={'source':site,'listing_kind':kind,'url':url,'scraped_at':collected or now(),
         'address':a.get('streetAddress'),'suburb':a.get('addressLocality'),
         'district':a.get('addressCounty'),'region':a.get('addressRegion'),
         'description':data.get('description') or obj.get('description'),
         'property_type':obj.get('@type'),'key_bedrooms':number(obj.get('numberOfBedrooms')),
         'key_bathrooms':number(obj.get('numberOfBathroomsTotal')),
         'latitude':number(geo.get('latitude')),'longitude':number(geo.get('longitude')),
         'year_built':number(obj.get('yearBuilt')),'images':obj.get('image') or data.get('image')}
    floor=obj.get('floorSize') or {}
    if isinstance(floor,dict) and floor.get('unitCode') in ('MTK',None):row['key_floor_area']=number(floor.get('value'))
    offers=data.get('offers') or obj.get('offers') or {}
    if isinstance(offers,dict) and offers.get('priceCurrency')=='NZD':
        if kind=='for_sale':row['price_numeric']=number(offers.get('price'))
        # A sale/rental offer is not proof of a historic sale or weekly rent.
    return {k:v for k,v in row.items() if present(v)}

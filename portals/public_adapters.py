"""Adapters for observed public embedded listing records, not private API calls."""
import re
from portals.page_data import number, date, pick


def normalise_public(n, raw):
    if raw['_apex_kind'] != n['listing_kind']:
        raise ValueError('Source listing category does not match requested category')
    if '_apex_trademe' in raw:
        a=raw['_apex_trademe']
        attrs={v['name']:v.get('value') for v in a.get('attributes',[]) if isinstance(v,dict) and v.get('name')}
        # Detail top-level suburb can be the district. Display attributes are authoritative here.
        location=str(attrs.get('location') or '').split(',')
        n.update(address=a.get('address') or (location[0].strip() if len(location)>=3 else None),
                 suburb=attrs.get('suburb') or a.get('suburb'),district=attrs.get('district') or a.get('district'),
                 region=attrs.get('region') or a.get('region'),listing_id=a['listingId'],
                 title=a.get('title'),description=a.get('body'),property_type=attrs.get('property_type') or a.get('propertyType'),
                 price_display=a.get('priceDisplay') or attrs.get('price'),open_homes=a.get('openHomes'),
                 agency=a.get('agency'),agents=(a.get('agency') or {}).get('agents'),
                 listed_date=date(str(a.get('startDate') or '').removeprefix('__date__:')))
        def count(value):
            match=re.fullmatch(r'(\d+)\s*(?:bedrooms?|bathrooms?)?',str(value or '').strip())
            return int(match[1]) if match else None
        n.update(key_bedrooms=count(attrs.get('bedrooms',a.get('bedrooms'))),key_bathrooms=count(attrs.get('bathrooms',a.get('bathrooms'))),
                 key_floor_area=number(attrs.get('floor_area',a.get('area'))),key_land_area=number(attrs.get('land_area',a.get('landArea'))),
                 cv_numeric=number(attrs.get('rateable_value_(rv)',a.get('rateableValue'))))
        parking=[number(attrs.get(k)) for k in ('garage_parking','off-street_parking')]
        n['key_carspaces']=a.get('totalParking') if a.get('totalParking') is not None else (sum(parking) if all(x is not None for x in parking) else None)
        geo=a.get('geographicLocation') or {}
        n.update(latitude=number(geo.get('latitude')),longitude=number(geo.get('longitude')))
        images=list(a.get('photoUrls') or [])
        for photo in a.get('photos') or []:
            value=pick(photo.get('value') or {},'fullSize','large','gallery')
            if value:images.append(value)
        n['images']=images
        # startPrice, auction date and localSales are not this home's sale price/date.
    else:
        a=raw['_apex_homes'];p=a['property_details'];address=str(p.get('display_address') or p.get('address') or '').split(',')
        n.update(address=address[0].strip(),suburb=p.get('suburb'),listing_id=a.get('listing_id'),property_id=a['property_id'],
                 title=p.get('headline'),description=a.get('description'),images=p.get('listing_images'),agents=a.get('agents'),agency=a.get('branches'),
                 price_display=a.get('display_price'),listed_date=date(a.get('date')),
                 key_bedrooms=number(p.get('num_bedrooms')),key_bathrooms=number(p.get('num_bathrooms')),key_carspaces=number(p.get('num_car_spaces')),
                 key_floor_area=number(p.get('floor_area')),key_land_area=number(p.get('land_area')),
                 cv_numeric=number(p.get('capital_value')),cv_date=date(p.get('current_revision_date')),
                 land_value_numeric=number(p.get('land_value')),improvement_value_numeric=number(p.get('improvement_value')),
                 homes_estimate=number(p.get('display_estimated_value_short')),homes_estimate_low=number(p.get('display_estimated_lower_value_short')),
                 homes_estimate_high=number(p.get('display_estimated_upper_value_short')),homes_estimate_date=date(p.get('estimated_value_revision_date')),
                 building_age_decade=p.get('decade_built'),legal_description=p.get('legal_description'),type_of_title=p.get('ownership_type'))
        # Only accept explicitly displayed district/region; don't equate city with district.
        if len(address)==4:n.update(district=address[2].strip(),region=address[3].strip())
        elif len(address)==3 and address[2].strip().casefold()=='auckland' and str(p.get('city','')).casefold()=='auckland':
            n['region']='Auckland'
        if n['listing_kind']=='sold':
            n.pop('listed_date',None)
            n['sold_date']=date(a.get('date'))
            # Only a disclosed numeric sold price qualifies. TBC stays unknown,
            # even when an estimate or internal numeric field is available.
            display=str(a.get('display_price') or '').strip()
            if re.fullmatch(r'\$[\d,]+(?:\.\d+)?',display):
                n['sale_price']=number(display)
            n['sale_record_basis']='public_sold_record'
        geo=a.get('point') or {};n.update(latitude=number(geo.get('lat')),longitude=number(geo.get('long')))
    display=str(n.get('price_display') or '').strip()
    if n['listing_kind']=='for_sale' and re.fullmatch(r'(?i)(?:asking price\s+)?\$[\d,.]+[km]?',display):
        n['price_numeric']=number(re.sub(r'(?i)^asking price\s+','',display))
    if n['listing_kind']=='rent' and re.fullmatch(r'(?i)\$[\d,.]+\s+per week',display):
        n['rent_weekly']=number(re.sub(r'(?i)\s+per week$','',display))
    return n

import json
from portals.hougarden_page import parse
URL='https://www.hougarden.com/nz/property/auckland/otahuhu/9-33-hutton-street/NukOr'
def page(url=URL):
    data={'url':url,'address':{'streetAddress':'9/33 Hutton Street','addressLocality':'Otahuhu'},'floorSize':{'unitCode':'MTK','value':'60'},'numberOfBedrooms':2}
    return '<script type="application/ld+json">'+json.dumps(data)+'</script><section><h3>Key Facts</h3><div><span>Type of title</span><span>Cross-Lease</span></div><div><span>Decade of construction</span><span>1974</span></div></section>'
def test_exact_page_facts_without_invented_land():
    item=parse(page(),URL)
    assert item['floor_area_m2']==60 and item['beds']==2
    assert item['building_age']=='1974' and item['type_of_title']=='Cross-Lease'
    assert 'land_area_m2' not in item
    assert item['source']=='hougarden'
def test_other_property_schema_rejected():
    assert parse(page(URL+'other'),URL) is None
def test_ambiguous_residences_rejected():
    assert parse(page()+page(),URL) is None

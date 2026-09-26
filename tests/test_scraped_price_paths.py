"""Synthetic advertised prices must survive daily pricing without fake discounts."""
import pytest
from models import PropertyForSale
from portals.direct import canonical
from portals.daily_pricing import reprice_live
from test_daily_pricing import _batches


@pytest.mark.parametrize('display,method,asking', [
    ('$950,000', 'fixed', 950000),
    ('$950,000 Negotiable', 'fixed', 950000),
    ('Enquiries over $950,000', 'offers over', 950000),
    ('Auction on 12 October', 'auction', None),
    ('By negotiation', 'negotiation', None),
    ('Deadline sale', 'deadline sale', None),
])
def test_scraped_method_survives_two_daily_prices(db_session, display, method, asking):
    scraped = canonical('oneroof', 'for_sale', 'https://www.oneroof.co.nz/example', {
        'priceBold': display, 'searchPrice': 700000})
    assert scraped['sale_method'] == method
    assert scraped.get('price_numeric') == asking
    fs, _ = _batches(db_session, 1)
    fs.status = 'published'
    listing = db_session.query(PropertyForSale).one()
    listing.asking_price = scraped.get('price_numeric')
    listing.price_numeric = scraped.get('price_numeric')
    listing.price_display = scraped['price_display']
    listing.sale_method = scraped['sale_method']
    db_session.commit()
    batch_id, listing_id = fs.id, listing.id
    for _ in range(2):
        reprice_live(db_session, batch_id)
        listing = db_session.get(PropertyForSale, listing_id)
        assert listing.fair_value is not None and listing.fair_value > 0
        assert listing.asking_price == asking
        if method != 'fixed':
            assert listing.margin is None
            assert not listing.is_underpriced

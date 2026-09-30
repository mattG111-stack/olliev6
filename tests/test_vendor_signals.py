from datetime import datetime, timezone, timedelta
import vendor_signals as v

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def sig(**kw):
    return v.compute(now=NOW, **kw)


def test_keyword_motivation():
    assert "mortgagee" in sig(description="Mortgagee sale, must be sold")["flags"]
    assert "present_all_offers" in sig(description="Vendor will present all offers")["flags"]
    assert "urgent" in sig(description="URGENT - priced to sell")["flags"]
    assert "estate" in sig(description="A deceased estate")["flags"]


def test_leaky_risk_and_resolution():
    assert "reclad" in sig(description="needs a full reclad, monolithic clad")["risk"]
    # already done -> not a risk
    assert sig(description="Fully re-clad in 2019 with weathertightness certificate")["risk"] == []


def test_structured_signals():
    assert "price_cut" in sig(prior_asking=1_200_000, asking=1_050_000)["flags"]
    assert "stale" in sig(first_seen_at=NOW - timedelta(days=95))["flags"]
    assert "negotiation" in sig(sale_method="By Negotiation")["flags"]


def test_clean_listing_has_no_flags():
    s = sig(description="Beautifully renovated home in a quiet cul-de-sac")
    assert s["flags"] == [] and s["risk"] == [] and s["motivated"] is False


def test_flags_string_is_searchable_and_bounded():
    s = sig(description="Mortgagee sale, monolithic clad needs reclad")
    fs = v.flags_string(s)
    assert "motivated" in fs and "mortgagee" in fs and "risk:reclad" in fs
    assert len(fs) <= 120

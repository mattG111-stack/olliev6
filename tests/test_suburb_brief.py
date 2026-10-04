from datetime import date
from types import SimpleNamespace as R
from assistant.suburb_brief import summarise, render, SuburbBrief

def row(address,day,price,days=10):
 return R(address=address,sold_date=day,sale_price=price,days_on_market=days)

def test_dedup_conflict_future_and_period():
 rows=[row('1 Road','2026-09-01',100),row('1 Road','2026-09-01',100),row('2 Road','2026-09-01',200),row('2 Road','2026-09-01',300),row('3 Road','2027-01-01',100),row('4 Road','bad',100)]
 d=summarise(rows,date(2026,10,4))
 assert d['count']==1 and d['median']==100
 assert d['change'] is None

def test_equal_windows_and_average():
 rows=[row(str(i),'2026-09-01',200,67 if i<2 else 68) for i in range(5)] + [row(str(i),'2026-03-01',100) for i in range(5)]
 d=summarise(rows,date(2026,10,4))
 assert d['change']==100 and d['average']==68 and d['count']==5
 text=render('Riverhead',d,27)
 assert '68 days (5 sales)' in text and 'apex-map' in text and '27' in text

def test_empty_explicit():
 text=render('Riverhead',summarise([],date(2026,10,4)),None)
 assert 'Unavailable' in text and 'No dated sales' in text

def test_wrapper_preserves_result():
 b=SuburbBrief()
 assert b.wrap(lambda n,a:'{}')('suburb_days_to_sell',{'suburb':'Riverhead'})=='{}'
 assert b.suburb=='Riverhead'

def test_only_one_map(monkeypatch):
 import assistant.suburb_brief as m
 monkeypatch.setattr(m,'load_brief',lambda s:'Snapshot\n```apex-map\n{}\n```')
 b=SuburbBrief(); b.suburb='Riverhead'
 text='Facts\n```apex-map\n{}\n```\n```apex-chart\n{}\n```\n```apex-map\n{}\n```'
 result=b.append(text)
 assert result.count('```apex-map')==1
 assert 'Facts' in result and '```apex-chart' in result

def test_failed_snapshot_keeps_map(monkeypatch):
 import assistant.suburb_brief as m
 def fail(s): raise RuntimeError()
 monkeypatch.setattr(m,'load_brief',fail)
 b=SuburbBrief(); b.suburb='Riverhead'
 text='```apex-map\n{}\n```'
 assert text in b.append(text)

def test_comparison_retains_both_suburbs_once(monkeypatch):
 import assistant.suburb_brief as m
 monkeypatch.setattr(m,'load_brief',lambda s:s+'\n```apex-map\n{}\n```')
 b=SuburbBrief(); call=b.wrap(lambda n,a:'{}')
 for suburb in ['Riverhead','Kumeu','riverhead']:
  call('suburb_days_to_sell',{'suburb':suburb})
 text=b.append('Comparison')
 assert text.count('```apex-map')==2
 assert 'Riverhead' in text and 'Kumeu' in text

def test_address_uses_resolved_suburb():
 b=SuburbBrief()
 b.wrap(lambda n,a:'{"suburb":"Riverhead"}')('find_address',{'suburb':'River'})
 assert b.suburb=='Riverhead'

def test_ambiguous_address_does_not_attach_guessed_area():
 b=SuburbBrief()
 b.wrap(lambda n,a:'Which suburb?')('find_address',{'suburb':'River'})
 assert b.append('Which suburb?')=='Which suburb?'

def test_budget_and_competing_listing_searches_capture_area():
 for args in [{'suburb':'Riverhead','max_price':1500000},{'suburb':'Riverhead','beds':4}]:
  b=SuburbBrief(); b.wrap(lambda n,a:'[]')('search_listings',args)
  assert b.suburb=='Riverhead'

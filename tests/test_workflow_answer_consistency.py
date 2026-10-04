from assistant.shortlist import render_shortlist
from assistant.suburb_brief import SuburbBrief

def test_budget_excludes_unpriced_and_rounds():
 rows=[dict(id=1,address='One',asking_price=1049000,fair_value=1174981),dict(id=2,address='Two',asking_price=None,fair_value=1200000)]
 text=render_shortlist('[One](/property/1) [Two](/property/2)',rows,3,'Find three homes under $1.5 million')
 assert '[Two]' not in text and '$1,180,000' in text and '$131,000' in text

def test_no_budget_match():
 text=render_shortlist('[One](/property/1)',[dict(id=1,address='One',asking_price=None,fair_value=1000000)],3,'under $1.5m')
 assert 'excluded' in text

def test_comparison_replaces_inconsistent_model_prose(monkeypatch):
 import assistant.suburb_brief as m
 monkeypatch.setattr(m,'load_brief',lambda s:'Stats '+s)
 b=SuburbBrief('Compare Riverhead and Kumeu'); b.suburb='Kumeu'; b.suburbs=['Riverhead','Kumeu']
 text=b.append('Wrong yearly comparison')
 assert 'Wrong' not in text and 'Riverhead' in text and 'Kumeu' in text and '180-day' in text

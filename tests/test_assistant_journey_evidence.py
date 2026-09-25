"""Synthetic provider-boundary tests; no paid calls or production writes."""
import json
from types import SimpleNamespace
from assistant import agent, tools
from assistant.providers import Result
from models import ImportBatch, PropertyForSale


def test_parking_and_age_are_not_presented_as_verified_condition(db_session):
    batch = ImportBatch(batch_type='for_sale', filename='synthetic.csv', is_active=True)
    db_session.add(batch); db_session.flush()
    home = PropertyForSale(import_batch_id=batch.id, address='1 Synthetic Road',
                           cars=7, building_age='2020s', asking_price=700000)
    db_session.add(home); db_session.commit()
    result = json.loads(tools.get_property(home.id))
    assert result['parking_evidence'] == {'recorded_car_spaces': 7, 'enclosed_garage': 'not verified'}
    assert result['condition'] == 'not verified'
    assert 'not condition evidence' in result['build_date_precision']


def test_budget_followup_bounds_provider_output_from_current_evidence(monkeypatch):
    monkeypatch.setattr(agent.keys, 'decrypt', lambda _: 'synthetic-not-a-real-key')
    records = [{'id': i, 'address': f'{i} Synthetic Road', 'asking_price': 600000+i,
                'fair_value': 700000, 'beds': 3, 'baths': 1} for i in range(1, 5)]
    monkeypatch.setattr(agent, 'dispatch', lambda *_: json.dumps({'listings': records}))
    def provider(**kwargs):
        result = json.loads(kwargs['dispatch']('search_listings', {}))
        assert result['shortlist_limit'] == 3
        assert len(result['listings']) == 4  # all candidates available to analyse
        assert kwargs['messages'][-1]['content'].startswith('Drop my budget')
        assert 'Glen Eden' in kwargs['messages'][0]['content']
        return Result(text=' '.join(f'[{r["address"]}](/property/{r["id"]})' for r in records))
    monkeypatch.setattr(agent.providers, 'run', provider)
    user = SimpleNamespace(llm_provider='anthropic', llm_api_key_encrypted='synthetic')
    history = [agent.Turn(role='user', content='Find my three best options in Glen Eden, 3 bedrooms under $900,000')]
    answer = agent.ask(user, 'Drop my budget to $800,000 and keep everything else the same', history)
    assert answer.text.count('](/property/') == 3
    assert '4 Synthetic Road' not in answer.text
    assert '$600,001' in answer.text


def test_explicit_count_followup_keeps_prior_selection_and_requirement_disclosures(monkeypatch):
    monkeypatch.setattr(agent.keys, 'decrypt', lambda _: 'synthetic-not-a-real-key')
    records = [{'id': i, 'address': f'{i} Synthetic Road', 'asking_price': 600000+i,
                'fair_value': 700000, 'beds': 3, 'baths': 1} for i in (1, 2, 4)]
    monkeypatch.setattr(agent, 'dispatch', lambda *_: json.dumps({'listings': records}))
    def provider(**kwargs):
        kwargs['dispatch']('search_listings', {})
        return Result(text=' '.join(f'[{r["address"]}](/property/{r["id"]})' for r in records))
    monkeypatch.setattr(agent.providers, 'run', provider)
    history = [agent.Turn(role='user', content='Find three houses with outdoor space, a garage and no renovation'),
               agent.Turn(role='assistant', content='[One](/property/1) [Two](/property/2) [Three](/property/3)')]
    user = SimpleNamespace(llm_provider='anthropic', llm_api_key_encrypted='synthetic')
    answer = agent.ask(user, 'Drop my budget and keep everything else the same. Show me three properties and chart asking prices.', history)
    assert '2 retained and 1 new' in answer.text
    assert 'car spaces do not confirm an enclosed garage' in answer.text
    assert '```apex-chart' in answer.text


def test_new_search_does_not_inherit_old_preferences_or_comparison():
    from assistant.shortlist import shortlist_context
    history = [agent.Turn(role='user', content='Find three houses with a garage'),
               agent.Turn(role='assistant', content='[One](/property/1)')]
    assert shortlist_context('New search: find three apartments', history) == ('New search: find three apartments', None)
    history += [agent.Turn(role='user', content='New search: find three apartments'),
                agent.Turn(role='assistant', content='[Two](/property/2)')]
    context, ids = shortlist_context('Keep everything the same but make it cheaper', history)
    assert 'garage' not in context
    assert ids == {2}

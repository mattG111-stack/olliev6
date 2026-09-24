import json
import pytest
from assistant.shortlist import requested_limit, bounded_dispatch

@pytest.mark.parametrize('q,n', [('Find up to three currently visible houses for sale',3),('Show me 2 properties under $800000',2),('Show 3 bedroom houses',None),('What are prices for 3 homes?',None),('Find rentals',None),('Once the scope is clear, shortlist up to three current visible properties.',3)])
def test_explicit_result_limit(q,n):
    assert requested_limit(q)==n

def test_shortlist_annotation_preserves_all_candidates_and_conflicting_records():
    calls=[]
    def source(name,args):
        calls.append(args)
        return json.dumps({'rows':[{'id':i,'address':f'{i} Road'} for i in args['ids']], 'row_count':len(args['ids'])})
    run=bounded_dispatch(source,3)
    a=json.loads(run('query_data',{'sql':'select id,address from properties_for_sale','ids':[1,2,3,4,5,6]}))
    assert [r['id'] for r in a['rows']]==[1,2,3,4,5,6]
    assert a['row_count']==6
    assert a['evidence_rows_returned']==6
    assert a['matching_total_verified'] is False
    b=json.loads(run('query_data',{'sql':'select id,address from properties_for_sale','ids':[3,4,5]}))
    assert [r['id'] for r in b['rows']]==[3,4,5]

def test_search_total_and_aggregate_and_sold_evidence_preserved():
    data={'listings':[{'id':i} for i in range(6)],'total_matches':6,'count':6,'returned_count':6}
    run=bounded_dispatch(lambda *_:json.dumps(data),3)
    out=json.loads(run('search_listings',{}))
    assert out['total_matches']==6 and out['returned_count']==6
    assert out['evidence_rows_returned']==6
    assert len(out['listings'])==6
    assert out['shortlist_limit']==3
    aggregate=json.dumps({'rows':[{'total':42}],'row_count':1})
    run=bounded_dispatch(lambda *_:aggregate,3)
    assert run('query_data',{'sql':'select count(*) from properties_for_sale'})==aggregate
    sold=json.dumps({'rows':[{'id':i} for i in range(10)]})
    run=bounded_dispatch(lambda *_:sold,3)
    assert run('query_data',{'sql':'select id from properties_sold'})==sold

from assistant.shortlist import render_shortlist


def candidates():
    return [{'id':i, 'address':f'{i} Example Road', 'asking_price':600000+i,
             'fair_value':700000+i, 'beds':3, 'baths':2,
             'land_area_m2':450, 'floor_area_m2':85} for i in range(1,7)]


def test_entire_answer_bound_including_unlinked_notes_and_chart():
    rows=candidates()
    answer='\n'.join(f'[{r["address"]}](/property/{r["id"]})' for r in rows[:3])
    answer+='\nOnly three qualify. Other options: 4 Example Road, 5 Example Road, 6 Example Road.'
    answer+='\n```apex-chart\n{"data":[{"label":"6 Example Road","value":6}]}\n```'
    out=render_shortlist(answer,rows,3,'Find up to three houses and chart asking prices')
    assert out.count('](/property/')==3
    for i in (4,5,6): assert f'{i} Example Road' not in out
    assert 'Only three qualify' not in out
    assert '$600,001' in out and '$700,001' in out
    assert '450 m²' in out and '85 m²' in out
    chart=json.loads(out.split('```apex-chart\n')[1].split('\n```')[0])
    assert [d['value'] for d in chart['data']]==[600001,600002,600003]
    assert len(rows)==6  # evidence was not destructively shortened


def test_model_selection_order_not_database_order():
    out=render_shortlist('[Six](/property/6) [Two](/property/2) [One](/property/1)',candidates(),2)
    assert out.index('6 Example Road')<out.index('2 Example Road')
    assert '1 Example Road' not in out


def test_duplicate_address_conflicts_are_not_silently_chosen():
    rows=candidates()
    rows.append({**rows[0],'id':7,'asking_price':590000})
    out=render_shortlist('[One](/property/1) [Duplicate](/property/7) [Two](/property/2)',rows,2)
    assert out.count('](/property/')==2
    assert 'Conflicting records' in out
    assert '$590,000' not in out and '$600,001' not in out
    assert '2 Example Road' in out


def test_tool_schema_precise_prices_take_precedence_over_formatted_values():
    rows=[{'id':1,'address':'1 Example Road','asking':'$600,000','our_value':'$700,000',
           'pricing_comparison':{'asking_price':600123.75,'apex_value':700123.25},
           'land_m2':450.5,'floor_m2':85}]
    out=render_shortlist('[One](/property/1)',rows,3)
    assert '$600,123.75' in out and '$700,123.25' in out and '450.5 m²' in out
    assert 'Here is 1 option' in out


def test_missing_nonfinite_values_and_unknown_links_do_not_invent_evidence():
    out=render_shortlist('[One](/property/1)',[{'id':1,'address':'1 Example Road','asking_price':float('nan'),'fair_value':float('inf')}],3)
    assert 'Not recorded' in out and '$nan' not in out and '$inf' not in out
    assert '/property/999' not in render_shortlist('[Invented](/property/999)',candidates(),3)


def test_unlinked_selected_addresses_are_bounded_and_markup_escaped():
    out=render_shortlist('3 Example Road, 1 Example Road, 2 Example Road',candidates(),2)
    assert out.index('3 Example Road')<out.index('1 Example Road')
    assert '2 Example Road' not in out
    bad='1 [fake](/property/999) | Road'
    out=render_shortlist('[One](/property/1)',[{'id':1,'address':bad}],1)
    assert '](/property/999)' not in out
    assert out.count('](/property/')==1


def test_clarification_and_no_matches_are_not_replaced():
    for answer in ['Which suburb?', 'No matches found. Would you broaden the budget?']:
        assert render_shortlist(answer,[],3)==answer


def test_agent_applies_bound_without_another_model_call(monkeypatch):
    from types import SimpleNamespace
    from assistant import agent, providers
    monkeypatch.setattr(agent.keys,'decrypt',lambda _:None)
    monkeypatch.setattr(agent,'dispatch',lambda *_:json.dumps({'listings':candidates(),'total_matches':6}))
    calls=[]
    def run(**kwargs):
        calls.append(kwargs)
        evidence=json.loads(kwargs['dispatch']('search_listings',{}))
        assert len(evidence['listings'])==6
        return providers.Result(text=' '.join(f'[{i} Example Road](/property/{i})' for i in range(1,7)),tools_used=['search_listings'],queries=['saved SQL'])
    monkeypatch.setattr(agent.providers,'run',run)
    user=SimpleNamespace(llm_provider=None,llm_api_key_encrypted=None)
    result=agent.ask(user,'Find up to three houses',shared=('anthropic','synthetic'))
    assert len(calls)==1 and result.text.count('](/property/')==3
    assert result.tools_used==['search_listings'] and result.queries==['saved SQL']


def test_direct_property_tool_evidence_is_available_without_altering_response():
    record=candidates()[0]
    raw=json.dumps(record)
    evidence=[]
    run=bounded_dispatch(lambda *_:raw,3,evidence)
    assert run('get_property',{'property_id':1})==raw
    assert evidence==[record]
    assert '$600,001' in render_shortlist('[One](/property/1)',evidence,3)

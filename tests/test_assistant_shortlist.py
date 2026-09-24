import json
import pytest
from assistant.shortlist import requested_limit, bounded_dispatch

@pytest.mark.parametrize('q,n', [('Find up to three currently visible houses for sale',3),('Show me 2 properties under $800000',2),('Show 3 bedroom houses',None),('What are prices for 3 homes?',None),('Find rentals',None)])
def test_explicit_result_limit(q,n):
    assert requested_limit(q)==n

def test_sql_evidence_caps_identities_across_calls_without_changing_totals():
    calls=[]
    def source(name,args):
        calls.append(args)
        return json.dumps({'rows':[{'id':i,'address':f'{i} Road'} for i in args['ids']], 'row_count':len(args['ids'])})
    run=bounded_dispatch(source,3)
    a=json.loads(run('query_data',{'sql':'select id,address from properties_for_sale','ids':[1,2,3,4,5,6]}))
    assert [r['id'] for r in a['rows']]==[1,2,3]
    assert a['row_count']==3 and a['shortlist_truncated']
    b=json.loads(run('query_data',{'sql':'select id,address from properties_for_sale','ids':[3,4,5]}))
    assert [r['id'] for r in b['rows']]==[3]

def test_search_total_and_aggregate_and_sold_evidence_preserved():
    data={'listings':[{'id':i} for i in range(6)],'total_matches':6,'count':6,'returned_count':6}
    run=bounded_dispatch(lambda *_:json.dumps(data),3)
    out=json.loads(run('search_listings',{}))
    assert out['total_matches']==6 and out['returned_count']==3
    aggregate=json.dumps({'rows':[{'total':42}],'row_count':1})
    run=bounded_dispatch(lambda *_:aggregate,3)
    assert run('query_data',{'sql':'select count(*) from properties_for_sale'})==aggregate
    sold=json.dumps({'rows':[{'id':i} for i in range(10)]})
    run=bounded_dispatch(lambda *_:sold,3)
    assert run('query_data',{'sql':'select id from properties_sold'})==sold

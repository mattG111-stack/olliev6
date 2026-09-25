"""Verify the provider wire request and preserve research defaults without paid calls."""
import sys
from types import SimpleNamespace
import pytest
from assistant import providers as P


@pytest.mark.parametrize('effort,expected', [('high','high'),('xhigh','xhigh'),('invalid','xhigh')])
def test_shortlist_effort_reaches_provider_with_adaptive_reasoning(monkeypatch, effort, expected):
    seen = []
    class Client:
        def __init__(self, **kwargs):
            self.messages = self
        def with_options(self, **kwargs):
            return self
        def create(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(type='text',text='Recorded answer')])
    monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(Anthropic=Client))
    out = P.run('anthropic','synthetic','system',[{'role':'user','content':'Compare'}],[],lambda *_:'',effort=effort)
    assert out.text == 'Recorded answer'
    assert seen[0]['output_config']['effort'] == expected
    assert seen[0]['thinking'] == {'type':'adaptive'}
    assert seen[0]['max_tokens'] == P.MAX_TOKENS

import json
from types import SimpleNamespace

import pytest

from assistant.investigation import InvestigationEvidence


QUESTION = 'Investigate only Apex property ID 42, selected from the latest shortlist (IDs 41, 42).'


def record(id=42):
    return json.dumps({'id': id, 'apex_url': f'/property/{id}', 'address': '[unsafe](https://example.test)'})


def test_verified_selected_record_link_added_without_imported_markdown():
    evidence = InvestigationEvidence(QUESTION)
    raw = record()
    assert evidence.wrap(lambda *_: raw)('get_property', {'property_id':42}) == raw
    answer = evidence.link_answer('Here is the assessment.')
    assert answer.endswith('[View the checked property record](/property/42)')
    assert 'example.test' not in answer
    assert evidence.link_answer(answer) == answer


@pytest.mark.parametrize('raw', [record(41), 'No listing with id 42.', '{}', '[]',
    json.dumps({'id':42, 'apex_url':'/property/41'}),
    json.dumps({'id':'42', 'apex_url':'/property/42'})])
def test_missing_mismatched_or_invalid_record_cannot_create_link(raw):
    evidence = InvestigationEvidence(QUESTION)
    evidence.wrap(lambda *_: raw)('get_property', {})
    assert evidence.link_answer('Answer') == 'Answer'


def test_no_links_from_other_tools_or_unrelated_questions():
    for question, tool in [(QUESTION, 'query_data'), ('Compare properties 41 and 42', 'get_property')]:
        evidence = InvestigationEvidence(question)
        evidence.wrap(lambda *_: record())(tool, {})
        assert evidence.link_answer('Answer') == 'Answer'


def test_each_turn_requires_its_own_evidence():
    first = InvestigationEvidence(QUESTION)
    first.wrap(lambda *_: record())('get_property', {})
    assert '/property/42' in first.link_answer('Answer')
    assert InvestigationEvidence(QUESTION).link_answer('Answer') == 'Answer'
    assert first.link_answer('') == ''


def test_agent_wires_real_dispatch_result_to_final_answer(monkeypatch):
    from assistant import agent
    from assistant.providers import Result
    monkeypatch.setattr(agent.keys, 'decrypt', lambda _: 'test-key')
    monkeypatch.setattr(agent, 'dispatch', lambda *_: record())
    def run(**kwargs):
        kwargs['dispatch']('get_property', {'property_id':42})
        return Result(text='Assessment', tools_used=['get_property'], iterations=2)
    monkeypatch.setattr(agent.providers, 'run', run)
    user = SimpleNamespace(llm_provider='anthropic', llm_api_key_encrypted='synthetic')
    result = agent.ask(user, QUESTION)
    assert result.text.endswith('(/property/42)')
    assert result.tools_used == ['get_property']

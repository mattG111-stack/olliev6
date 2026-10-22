from types import SimpleNamespace
import pytest
import notify


@pytest.mark.parametrize('channel', ['email','sms'])
@pytest.mark.parametrize('failure', ['unconfigured','rejected','exception'])
def test_delivery_failures_never_log_messages_codes_or_credentials(monkeypatch, caplog, channel, failure):
    configured = failure != 'unconfigured'
    for key in ['resend_api_key','twilio_account_sid','twilio_auth_token','twilio_from_number']:
        monkeypatch.setattr(notify.settings,key,'synthetic-provider-credential' if configured else '')
    secret='synthetic-code-738192'
    address='synthetic-recipient@example.com'
    def post(*args,**kwargs):
        if failure=='exception':raise RuntimeError(secret+' synthetic-provider-credential')
        return SimpleNamespace(status_code=403,text=secret+' synthetic-provider-credential')
    monkeypatch.setattr(notify.httpx,'post',post)
    result=(notify.send_email(address,secret,secret,secret) if channel=='email'
            else notify.send_sms(address,secret))
    assert result is False
    assert secret not in caplog.text and address not in caplog.text
    assert 'synthetic-provider-credential' not in caplog.text
    assert 'delivery' in caplog.text.lower()

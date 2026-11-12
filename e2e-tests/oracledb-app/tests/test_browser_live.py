"""Boundary controls for the internal browser demo; no real provider requests."""
import pytest
from browser_live import query_options, safe_origin


@pytest.mark.parametrize('value', [{}, [], {'query': ''}, {'query': ' '}, {'query': 'x'*1001},
                                  {'query': 1}, {'query': 'ok', 'refresh': 'true'},
                                  {'query': 'ok', 'model': 'override'}])
def test_invalid_browser_payload_is_rejected(value):
    with pytest.raises(ValueError):
        query_options(value)


def test_valid_question_is_trimmed_without_changing_its_content():
    assert query_options({'query': '  Oracle vectors?  '}) == ('Oracle vectors?', False)
    assert query_options({'query': 'fresh', 'refresh': True}) == ('fresh', True)


@pytest.mark.parametrize('headers', [
    {'Host':'127.0.0.1:4111','Origin':'https://external.invalid'},
    {'Host':'external.invalid','Origin':'http://127.0.0.1:4111'},
    {'Host':'127.0.0.1:4112','Origin':'http://127.0.0.1:4112'},
    {'Host':'127.0.0.1:4111'},
])
def test_cross_origin_or_wrong_port_requests_are_rejected(headers):
    assert not safe_origin(headers, 4111)


def test_forwarded_loopback_origins_are_allowed():
    for host in ['127.0.0.1:4111','localhost:4111']:
        assert safe_origin({'Host':host,'Origin':'http://'+host}, 4111)

import json
import os
os.environ.setdefault('DATABASE_URL', 'sqlite:///./test_requirements.db')
os.environ.setdefault('SECRET_KEY', 'test-secret')
from fastapi.testclient import TestClient
from app import app

PATH = '/static/address-selector/index.html'

def test_bitrix_post_renders_widget_and_safe_context():
    response = TestClient(app).post(PATH, headers={'Origin': 'https://b24-7h3u4x.bitrix24.ru'}, data={
        'PLACEMENT': 'CRM_DEAL_DETAIL_TAB', 'PLACEMENT_OPTIONS': json.dumps({'ID': '12550'}),
        'AUTH_ID': 'test-secret-never-echo', 'SERVER_ENDPOINT': 'https://example.invalid/'})
    assert response.status_code == 200
    assert 'text/html' in response.headers['content-type']
    assert 'window.__CTX__=' in response.text
    assert '12550' in response.text
    assert 'test-secret-never-echo' not in response.text
    assert 'https://example.invalid/' not in response.text

def test_widget_cannot_inject_script_through_context():
    response = TestClient(app).post(PATH, data={'PLACEMENT_OPTIONS': json.dumps({'ID': '</script><script>alert(777)</script>'}), 'PLACEMENT': '</script><script>alert(888)</script>'})
    assert response.status_code == 200
    assert 'alert(777)' not in response.text
    assert 'alert(888)' not in response.text

def test_csrf_stays_enabled_outside_exact_widget_post():
    client = TestClient(app)
    for path in ['/settings/vehicles', '/static/address-selector/other', PATH + '/unexpected']:
        assert client.post(path).status_code == 403
    assert client.put(PATH).status_code == 403

def test_widget_get_and_bad_json_are_safe():
    client = TestClient(app)
    assert client.get(PATH).status_code == 200
    assert client.post(PATH, data={'PLACEMENT_OPTIONS': 'bad json'}).status_code == 200

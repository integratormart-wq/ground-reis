import io

from backend import bitrix


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"result": {"types": []}}'


def test_http_post_accepts_full_generator_url(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _Response()

    monkeypatch.setattr(bitrix.urllib.request, "urlopen", fake_urlopen)

    response = bitrix._http_post(
        "https://example.bitrix24.ru/rest/226/secret-token/profile.json",
        "crm.type.list",
        {},
    )

    assert response == {"result": {"types": []}}
    assert captured["url"] == "https://example.bitrix24.ru/rest/226/secret-token/crm.type.list"

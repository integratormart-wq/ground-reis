import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_enrich.db")
os.environ.setdefault("SECRET_KEY", "test-secret")

from backend import bitrix


def _trip(parent_deal_id=None, overrides=None):
    """Элемент рейса (пухтовоз 1088) с пустыми полями + связью со сделкой."""
    item = {
        "id": 142,
        "parentId2": parent_deal_id,
        "companyId": None,
        "contactId": None,
        "ufCrm30_1786395144602": None,   # Объем
        "ufCrm30_1788102038144": None,   # Тоннаж
        "ufCrm30_1786395006418": None,   # Дата и время
        "ufCrm30_1786395026081": None,   # Адрес подачи
        "ufCrm30_1786395095607": None,   # Полигон
        "ufCrm30_1788118084754": None,   # Тип груза
        "ufCrm30_1789035566": None,      # Контакт на объекте
    }
    if overrides:
        item.update(overrides)
    return item


def _deal():
    return {
        "result": {
            "COMPANY_ID": "6412",
            "CONTACT_ID": "5992",
            "UF_CRM_1788949563512": "14.0",       # Объем
            "UF_CRM_1788949587990": "27.0",       # Тоннаж
            "UF_CRM_1733804506197": "2026-09-11T19:00:00+03:00",
            "UF_CRM_GROUND_ADDRESS_PODACHA": "Кирова 53а",
            "UF_CRM_GROUND_CONTACT_OBJECT": "Иван",
            "UF_CRM_1788718754770": "734",        # Полигон → Кабельгрупп
            "UF_CRM_1786903524677": "620",        # Тип мусора → Грунт
        }
    }


def test_enrich_fills_empty_fields_from_parent_deal(monkeypatch):
    trip = _trip(parent_deal_id="100")
    calls = {}

    monkeypatch.setattr(bitrix, "fetch_item", lambda w, eid, iid: trip)

    def fake_post(webhook, method, params):
        calls[method] = params
        if method == "crm.deal.get":
            return _deal()
        return {"result": True}

    monkeypatch.setattr(bitrix, "_http_post", fake_post)

    result = bitrix.enrich_trip_from_deal("https://example/rest/1/token/", 1088, 142)

    assert result["status"] == "success"
    updates = calls["crm.item.update"]["fields"]
    assert updates["companyId"] == 6412
    assert updates["contactId"] == 5992
    assert updates["ufCrm30_1786395144602"] == "14.0"
    assert updates["ufCrm30_1788102038144"] == "27.0"
    assert updates["ufCrm30_1786395006418"] == "2026-09-11T19:00:00+03:00"
    assert updates["ufCrm30_1786395026081"] == "Кирова 53а"
    assert updates["ufCrm30_1786395095607"] == "604"  # полигон по маппингу
    assert updates["ufCrm30_1788118084754"] == "674"  # тип груза по маппингу


def test_enrich_does_not_overwrite_existing_fields(monkeypatch):
    trip = _trip(parent_deal_id="100", overrides={
        "companyId": 999, "ufCrm30_1786395144602": "5.0",
    })
    calls = {}

    monkeypatch.setattr(bitrix, "fetch_item", lambda w, eid, iid: trip)
    def fake_post(webhook, method, params):
        calls[method] = params
        if method == "crm.deal.get":
            return _deal()
        return {"result": True}
    monkeypatch.setattr(bitrix, "_http_post", fake_post)

    result = bitrix.enrich_trip_from_deal("https://example/rest/1/token/", 1088, 142)

    updates = calls["crm.item.update"]["fields"]
    assert "companyId" not in updates              # уже заполнено — не трогаем
    assert "ufCrm30_1786395144602" not in updates  # уже заполнено — не трогаем
    assert updates["contactId"] == 5992            # пустое — заполняем


def test_enrich_skips_without_deal_linkage(monkeypatch):
    trip = _trip(parent_deal_id=None)
    monkeypatch.setattr(bitrix, "fetch_item", lambda w, eid, iid: trip)

    result = bitrix.enrich_trip_from_deal("https://example/rest/1/token/", 1088, 142)

    assert result["status"] == "skipped"
    assert result["reason"] == "no_deal_linkage"

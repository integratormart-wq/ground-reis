import os
from datetime import date

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_requirements.db")
os.environ.setdefault("SECRET_KEY", "test-secret")

from fastapi.testclient import TestClient
import app as app_module
from backend import models


def reset_db():
    models.Base.metadata.drop_all(bind=models.engine)
    models.Base.metadata.create_all(bind=models.engine)
    db = models.SessionLocal()
    admin = models.User(full_name="Админ", login="testadmin", password_hash=app_module.pwd_hash("pass"), role=models.UserRole.ADMIN, is_active=True)
    driver = models.User(full_name="Новый водитель", login="driver-new", password_hash=app_module.pwd_hash("pass"), role=models.UserRole.DRIVER, is_active=True)
    vt = models.VehicleType(name="Тестовый тип", kind=models.TripType.PUKHTOVOZ)
    db.add_all([admin, driver, vt]); db.commit()
    return db, admin, driver, vt


def client_as(user):
    app_module.app.dependency_overrides[app_module.get_current_user] = lambda: user
    return TestClient(app_module.app)


def test_settings_additions_appear_in_new_request_form():
    db, admin, driver, vt = reset_db()
    client = client_as(admin)
    assert client.post("/settings/vehicles", data={"name": "Новое авто", "plate": "А111АА78", "type_id": str(vt.id), "capacity": "18"}, follow_redirects=False).status_code == 302
    assert client.post("/settings/polygons", data={"name": "Полигон Север", "address": "ЛО"}, follow_redirects=False).status_code == 302
    assert client.post("/settings/tariffs", data={"title": "Новый тариф", "kind": "пухтовоз", "vehicle_type_id": str(vt.id), "formula": "trip", "trip_price": "5000", "is_active": "on"}, follow_redirects=False).status_code == 302
    page = client.get("/pukhtovoz/new")
    assert page.status_code == 200
    assert "Новое авто" in page.text
    assert "Полигон Север" in page.text
    assert "Новый тариф" in page.text
    assert "Новый водитель" in page.text
    db.close()


def test_request_form_and_reports_do_not_show_waste_bin_field():
    db, admin, *_ = reset_db()
    client = client_as(admin)
    assert "КБ мусора" not in client.get("/pukhtovoz/new").text
    assert "КБ мусора" not in client.get("/reports").text
    assert "КБ мусора" not in client.get("/polygons").text
    db.close()


def test_polygon_page_filters_selected_polygon_and_period_and_export_has_detail_rows():
    db, admin, driver, vt = reset_db()
    polygon_a = models.Polygon(name="Полигон А")
    polygon_b = models.Polygon(name="Полигон Б")
    vehicle = models.Vehicle(name="Авто", plate="В222ВВ78", type_id=vt.id)
    db.add_all([polygon_a, polygon_b, vehicle]); db.flush()
    db.add_all([
        models.TripRequest(number="П-1", planned_date=date(2026, 8, 1), driver_id=driver.id, vehicle_id=vehicle.id, polygon_id=polygon_a.id, volume=10, actual_volume=None, kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.ASSIGNED),
        models.TripRequest(number="П-2", planned_date=date(2026, 8, 10), driver_id=driver.id, vehicle_id=vehicle.id, polygon_id=polygon_a.id, volume=20, kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.LOGIST_CONFIRMED),
        models.TripRequest(number="П-3", planned_date=date(2026, 8, 10), driver_id=driver.id, vehicle_id=vehicle.id, polygon_id=polygon_b.id, volume=30, kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.LOGIST_CONFIRMED),
    ])
    db.commit()
    client = client_as(admin)
    page = client.get(f"/polygons?polygon_id={polygon_a.id}&date_from=2026-08-01&date_to=2026-08-05")
    assert page.status_code == 200
    assert "Полигон А" in page.text
    assert "<td>Полигон Б</td>" not in page.text
    assert ">1<" in page.text
    assert ">10.0<" in page.text or ">10<" in page.text
    export = client.get(f"/export/polygon.csv?polygon_id={polygon_a.id}&date_from=2026-08-01&date_to=2026-08-31")
    text = export.content.decode("utf-8-sig")
    assert export.status_code == 200
    assert "Номер" in text and "П-1" in text and "П-2" in text
    assert "П-3" not in text
    db.close()


def test_manifest_is_installable_and_has_maskable_icon():
    client = TestClient(app_module.app)
    manifest = client.get("/static/manifest.json").json()
    assert manifest["display"] == "standalone"
    assert manifest.get("id")
    assert all(icon.get("purpose") == "any" for icon in manifest["icons"])
    assert client.get("/static/sw.js").status_code == 200
    root_sw = client.get("/sw.js")
    assert root_sw.status_code == 200
    assert root_sw.headers.get("service-worker-allowed") == "/"
    assert "register('/sw.js')" in client.get("/login").text
    login_html = client.get("/login").text
    assert 'id="install-app"' in login_html
    assert 'id="install-app" class="btn btn-primary btn-sm" type="button" hidden' not in login_html
    assert "navigator.standalone === true" in login_html
    assert "installButton.remove()" in login_html


def test_bitrix_form_encoder_flattens_nested_fields():
    encoded = app_module.bitrix._encode_params({"entityTypeId": 1, "fields": {"title": "П-1", "ufVolumePlan": 12}})
    assert encoded["entityTypeId"] == 1
    assert encoded["fields[title]"] == "П-1"
    assert encoded["fields[ufVolumePlan]"] == 12
    event, item_id, entity_id = app_module.bitrix.extract_event_identifiers({
        "event": "ONCRMDYNAMICITEMUPDATE",
        "data[FIELDS][ID]": "900",
        "data[FIELDS][ENTITY_TYPE_ID]": "150",
    })
    assert (event, item_id, entity_id) == ("ONCRMDYNAMICITEMUPDATE", 900, 150)


def test_bitrix_outbound_add_then_update_without_duplicate(monkeypatch):
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто sync", plate="Е444ЕЕ78", type_id=vt.id)
    db.add(vehicle); db.flush()
    trip = models.TripRequest(number="П-SYNC", planned_date=date(2026, 8, 12), driver_id=driver.id, vehicle_id=vehicle.id, volume=12, kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.NEW)
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add_all([trip, settings]); db.commit()
    calls = []
    monkeypatch.setattr(app_module.bitrix, "resolve_process_entity", lambda url, kind: "150")
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {"title": {"title": "Название"}, "ufVolumePlan": {"title": "Объем"}})
    def fake_post(url, method, payload):
        calls.append((method, payload))
        return {"result": {"item": {"id": 901}}} if method == "crm.item.add" else {"result": {}}
    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)
    first = app_module.bitrix.sync_trip(trip, db, settings=settings)
    db.commit()
    second = app_module.bitrix.sync_trip(trip, db, settings=settings)
    assert first["action"] == "add" and second["action"] == "update"
    assert trip.bitrix_element_id == 901
    assert calls[0][0] == "crm.item.add" and calls[1][0] == "crm.item.update"
    assert calls[1][1]["id"] == 901
    db.close()


def test_bitrix_can_upsert_local_trip_from_smart_process(monkeypatch):
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто Б24", plate="С333СС78", type_id=vt.id)
    polygon = models.Polygon(name="Полигон Б24")
    db.add_all([vehicle, polygon]); db.commit()
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add(settings); db.commit()

    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"150": models.TripType.PUKHTOVOZ, "151": models.TripType.SAMOSVAL})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {
        "id": 900, "title": "П-Б24-900", "ufReisDate": "2026-08-10", "ufReisTime": "09:30",
        "ufDriver": driver.full_name, "ufVehicle": vehicle.name, "ufPolygon": polygon.name,
        "ufVolumePlan": 14, "ufKmPlan": 25, "ufStatus": "Новая"
    })
    result = app_module.bitrix.sync_from_bitrix(900, 150, db, settings=settings)
    db.commit()
    trip = db.query(models.TripRequest).filter(models.TripRequest.bitrix_element_id == 900).one()
    assert result["ok"] is True
    assert trip.number == "П-Б24-900"
    assert trip.kind == models.TripType.PUKHTOVOZ
    assert trip.volume == 14
    assert trip.driver_id == driver.id
    assert trip.vehicle_id == vehicle.id
    assert trip.polygon_id == polygon.id
    db.close()

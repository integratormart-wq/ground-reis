import os
import json
import base64
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
    return TestClient(app_module.app, headers={"Origin": "http://testserver"})


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


def test_polygon_creation_saves_address_contact_and_phone_from_both_entry_points():
    db, admin, *_ = reset_db()
    client = client_as(admin)

    polygons_page = client.get("/polygons")
    assert polygons_page.status_code == 200
    assert 'name="address"' in polygons_page.text
    assert 'name="contact"' in polygons_page.text
    assert 'name="phone"' in polygons_page.text

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert 'action="/settings/polygons"' in settings_page.text
    assert 'name="contact"' in settings_page.text
    assert 'name="phone"' in settings_page.text

    direct = client.post("/polygons", data={
        "name": "Полигон Контакт 1",
        "address": "Ленинградская область, 1",
        "contact": "Иван Иванов",
        "phone": "+79990000001",
    }, follow_redirects=False)
    assert direct.status_code == 302

    settings = client.post("/settings/polygons", data={
        "name": "Полигон Контакт 2",
        "address": "Ленинградская область, 2",
        "contact": "Петр Петров",
        "phone": "+79990000002",
    }, follow_redirects=False)
    assert settings.status_code == 302

    first = db.query(models.Polygon).filter_by(name="Полигон Контакт 1").one()
    second = db.query(models.Polygon).filter_by(name="Полигон Контакт 2").one()
    assert (first.address, first.contact, first.phone) == (
        "Ленинградская область, 1", "Иван Иванов", "+79990000001",
    )
    assert (second.address, second.contact, second.phone) == (
        "Ленинградская область, 2", "Петр Петров", "+79990000002",
    )
    db.close()


def test_new_request_form_displays_selected_polygon_contact_details():
    db, admin, *_ = reset_db()
    polygon = models.Polygon(
        name="Полигон с реквизитами",
        address="Ленинградская область, Полигонная 7",
        contact="Сергей Петров",
        phone="+79991234567",
    )
    db.add(polygon); db.commit()
    client = client_as(admin)

    page = client.get("/pukhtovoz/new")
    assert page.status_code == 200
    assert 'id="polygon_id"' in page.text
    assert "Адрес полигона" in page.text
    assert "Контактное лицо полигона" in page.text
    assert "Телефон полигона" in page.text
    assert "Ленинградская область, Полигонная 7" in page.text
    assert "Сергей Петров" in page.text
    assert "+79991234567" in page.text
    assert "syncPolygonDetails" in page.text
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


def test_polygon_filter_has_search_and_no_driver_field_and_delete_updates_totals():
    db, admin, driver, vt = reset_db()
    polygon = models.Polygon(name="Полигон Удаление")
    vehicle = models.Vehicle(name="Авто удаление", plate="Т555ТТ78", type_id=vt.id)
    db.add_all([polygon, vehicle]); db.flush()
    trip = models.TripRequest(
        number="П-DEL", planned_date=date(2026, 8, 11), driver_id=driver.id,
        vehicle_id=vehicle.id, polygon_id=polygon.id, volume=17,
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.ASSIGNED,
    )
    db.add(trip); db.commit()
    client = client_as(admin)

    page = client.get(f"/polygons?polygon_id={polygon.id}&date_from=2026-08-01&date_to=2026-08-31")
    assert page.status_code == 200
    assert 'type="submit">Поиск</button>' in page.text
    assert 'name="driver_id"' not in page.text
    assert ">1<" in page.text and (">17.0<" in page.text or ">17<" in page.text)

    deleted = client.post(f"/requests/{trip.id}/delete", follow_redirects=False)
    assert deleted.status_code == 302
    updated = client.get(f"/polygons?polygon_id={polygon.id}&date_from=2026-08-01&date_to=2026-08-31")
    assert ">0<" in updated.text
    assert db.query(models.TripRequest).filter(models.TripRequest.id == trip.id).first() is None
    db.close()


def test_pukhtovoz_and_samosval_filters_by_period_driver_and_status():
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто фильтр", plate="К777КК78", type_id=vt.id)
    db.add(vehicle); db.flush()
    db.add_all([
        models.TripRequest(number="П-FIND", planned_date=date(2026, 8, 5), driver_id=driver.id, vehicle_id=vehicle.id, kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.IN_WORK),
        models.TripRequest(number="П-OUT", planned_date=date(2026, 7, 5), driver_id=driver.id, vehicle_id=vehicle.id, kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.IN_WORK),
        models.TripRequest(number="С-FIND", planned_date=date(2026, 8, 6), driver_id=driver.id, vehicle_id=vehicle.id, kind=models.TripType.SAMOSVAL, status=models.RequestStatus.NEW),
    ])
    db.commit()
    client = client_as(admin)

    page = client.get(f"/pukhtovoz?date_from=2026-08-01&date_to=2026-08-31&driver_id={driver.id}&status_f={models.RequestStatus.IN_WORK.value}")
    assert page.status_code == 200
    assert "П-FIND" in page.text and "П-OUT" not in page.text and "С-FIND" not in page.text
    assert 'name="date_from"' in page.text and 'name="date_to"' in page.text
    assert 'name="driver_id"' in page.text
    assert 'type="submit">Поиск</button>' in page.text
    export = client.get(f"/export/requests.csv?kind=пухтовоз&date_from=2026-08-01&date_to=2026-08-31&driver_id={driver.id}&status_f={models.RequestStatus.IN_WORK.value}")
    export_text = export.content.decode("utf-8-sig")
    assert "П-FIND" in export_text and "П-OUT" not in export_text and "С-FIND" not in export_text

    samosval = client.get(f"/samosval?date_from=2026-08-01&date_to=2026-08-31&driver_id={driver.id}&status_f={models.RequestStatus.NEW.value}")
    assert "С-FIND" in samosval.text and "П-FIND" not in samosval.text
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


def test_healthz_reports_starting_then_ready():
    app_module._DB_READY.clear()
    starting = TestClient(app_module.app).get("/healthz")
    assert starting.status_code == 503 and starting.json() == {"status": "starting"}
    app_module._DB_READY.set()
    ready = TestClient(app_module.app).get("/healthz")
    assert ready.status_code == 200 and ready.json() == {"status": "ready"}


def test_healthz_exposes_safe_render_commit_marker(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "b163b972ea05eb1662090a18ca04ee1216a303bf")
    app_module._DB_READY.set()
    response = TestClient(app_module.app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "commit": "b163b972ea05"}


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


def test_bitrix_full_trip_contract_includes_operational_and_payment_fields():
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто полный обмен", plate="А101АА78", type_id=vt.id)
    cargo = models.CargoType(name="Грунт", unit="м³")
    tariff = models.Tariff(
        title="Полный тариф", vehicle_type_id=vt.id, kind=models.TripType.PUKHTOVOZ,
        trip_price=7000, formula="trip", is_active=True,
    )
    db.add_all([vehicle, cargo, tariff]); db.flush()
    trip = models.TripRequest(
        number="П-FULL", planned_date=date(2026, 8, 12), planned_time="10:30",
        driver_id=driver.id, vehicle_id=vehicle.id, cargo_type_id=cargo.id,
        tariff_id=tariff.id, trips_count=3, km=0, volume=0, actual_km=0,
        actual_volume=0, sum_trip=21000, sum_driver=18000, waste_bin_count=2,
        tonnage=12.5, actual_tonnage=11.75,
        site_contact_name="Иван", site_contact_phone="+79990000000",
        site_contact_comment="Позвонить заранее",
        started_at=app_module.datetime(2026, 8, 12, 10, 45),
        finished_at=app_module.datetime(2026, 8, 12, 12, 15),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.DRIVER_COMPLETED,
    )
    db.add(trip); db.commit()
    fields = app_module.bitrix.build_fields(trip)
    assert fields["ufReisDate"] == "2026-08-12T10:30"
    assert "ufReisTime" not in fields
    assert fields["ufTripsCount"] == 3
    assert fields["ufCargoType"] == "Грунт"
    assert fields["ufTariff"] == "Полный тариф"
    assert fields["ufSumTrip"] == 21000
    assert fields["ufSumDriver"] == 18000
    assert fields["ufWasteBinCount"] == 2
    assert fields["ufStartedAt"] == "2026-08-12T10:45:00"
    assert fields["ufFinishedAt"] == "2026-08-12T12:15:00"
    assert fields["ufKmPlan"] == 0 and fields["ufVolumePlan"] == 0
    assert fields["ufKmFact"] == 0 and fields["ufVolumeFact"] == 0
    assert fields["ufTonnagePlan"] == 12.5
    assert fields["ufTonnageFact"] == 11.75
    assert fields["ufSiteContact"] == "Иван"
    assert fields["ufSitePhone"] == "+79990000000"
    assert fields["ufSiteContactComment"] == "Позвонить заранее"
    db.close()


def test_bitrix_customer_identity_and_driver_facts_round_trip(monkeypatch):
    db, admin, driver, vt = reset_db()
    customer = models.Customer(
        name="Клиент старое имя", inn="7812345678", bitrix_company_id=4455,
    )
    trip = models.TripRequest(
        number="П-CUSTOMER-ID", planned_date=date(2026, 8, 12), customer=customer,
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.IN_WORK,
        is_empty_run=True, empty_run_comment="Возврат без груза",
        has_downtime=True, downtime_minutes=45, downtime_comment="Очередь",
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True,
    )
    db.add_all([customer, trip, settings]); db.commit()

    fields = app_module.bitrix.build_fields(trip)
    assert fields["ufCustomerBitrixId"] == 4455
    assert fields["ufCustomerInn"] == "7812345678"
    assert fields["ufEmptyRun"] == "Да" and fields["ufHasDowntime"] == "Да"

    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"150": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 990, "title": "П-CUSTOMER-ID", "ufCustomer": "Клиент новое имя",
        "ufCustomerBitrixId": "4455", "ufCustomerInn": "7812345678",
        "ufEmptyRun": "Да", "ufEmptyRunComment": "Новая причина",
        "ufHasDowntime": "Y", "ufDowntimeMinutes": "60", "ufDowntimeComment": "Весы",
    })
    result = app_module.bitrix.sync_from_bitrix(990, 150, db, settings=settings)
    db.commit(); db.refresh(trip); db.refresh(customer)
    assert result["ok"] is True
    assert trip.customer_id == customer.id
    assert db.query(models.Customer).count() == 1
    assert customer.name == "Клиент новое имя" and customer.inn == "7812345678"
    assert trip.is_empty_run is True and trip.empty_run_comment == "Новая причина"
    assert trip.has_downtime is True and trip.downtime_minutes == 60 and trip.downtime_comment == "Весы"
    db.close()


def test_bitrix_customer_falls_back_to_inn_without_duplicate(monkeypatch):
    db, admin, driver, vt = reset_db()
    customer = models.Customer(name="Клиент по ИНН", inn="7800000000")
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add_all([customer, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"150": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 991, "title": "П-INN", "ufCustomer": "Переименованный клиент",
        "ufCustomerInn": "7800000000", "ufCustomerBitrixId": "7788",
    })
    app_module.bitrix.sync_from_bitrix(991, 150, db, settings=settings)
    db.commit(); db.refresh(customer)
    trip = db.query(models.TripRequest).filter_by(bitrix_element_id=991).one()
    assert trip.customer_id == customer.id and db.query(models.Customer).count() == 1
    assert customer.bitrix_company_id == 7788 and customer.name == "Переименованный клиент"
    db.close()


def test_bitrix_customer_identity_conflict_does_not_block_trip_sync(monkeypatch):
    db, admin, driver, vt = reset_db()
    id_owner = models.Customer(name="Клиент по ID", bitrix_company_id=4455)
    name_owner = models.Customer(name="ООО Ромашка")
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add_all([id_owner, name_owner, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"150": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 992, "title": "П-NAME-CONFLICT", "ufCustomer": "ооо, ромашка!!!",
        "ufCustomerBitrixId": "4455",
    })
    result = app_module.bitrix.sync_from_bitrix(992, 150, db, settings=settings)
    assert result["ok"] is True
    db.commit(); db.expire_all()
    saved_trip = db.query(models.TripRequest).filter_by(bitrix_element_id=992, bitrix_entity_type_id=150).one()
    assert saved_trip.customer_id == id_owner.id
    assert db.query(models.Customer).filter_by(id=id_owner.id).one().name == "Клиент по ID"
    db.close()


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


def test_bitrix_outbound_add_without_remote_id_fails_closed(monkeypatch):
    db, admin, driver, vt = reset_db()
    trip = models.TripRequest(
        number="П-NO-ID", planned_date=date(2026, 8, 12),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.NEW,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True,
    )
    db.add_all([trip, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_entity", lambda url, kind: "150")
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {"title": {"title": "Название"}})
    monkeypatch.setattr(app_module.bitrix, "_http_post", lambda url, method, payload: {"result": {}})
    assert app_module.bitrix.sync_trip(trip, db, settings=settings) == {
        "error": "bitrix_item_id_missing", "action": "add",
    }
    assert trip.bitrix_element_id is None
    db.close()


def test_customer_settings_deduplicate_normalized_names():
    db, admin, *_ = reset_db()
    db.add(models.Customer(name="ООО Ромашка")); db.commit()
    response = client_as(admin).post(
        "/settings/customers",
        data={"name": "  ооо, ромашка!!!  ", "address": "", "inn": "", "bitrix_company_id": ""},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert db.query(models.Customer).count() == 1
    db.close()


def test_customer_edit_rejects_normalized_duplicate_name():
    db, admin, *_ = reset_db()
    first = models.Customer(name="ООО Ромашка")
    second = models.Customer(name="Другой заказчик")
    db.add_all([first, second]); db.commit()
    response = client_as(admin).post(
        f"/settings/customers/{second.id}/edit",
        data={"name": "ооо, ромашка!!!", "address": "", "inn": "", "bitrix_company_id": ""},
        follow_redirects=False,
    )
    assert response.status_code == 400
    db.expire_all()
    assert db.query(models.Customer).filter_by(id=second.id).one().name == "Другой заказчик"
    db.close()


def test_polygon_edit_rejects_non_http_navigator_url():
    db, admin, *_ = reset_db()
    polygon = models.Polygon(name="Опасный полигон")
    db.add(polygon); db.commit()
    response = client_as(admin).post(
        f"/settings/polygons/{polygon.id}/edit",
        data={
            "name": polygon.name, "address": "", "entry_notes": "",
            "navigator_url": "javascript:alert(1)", "calculation_method": "volume",
            "volume_rate": "0", "tonnage_rate": "0", "waste_types": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    db.expire_all()
    assert db.query(models.Polygon).filter_by(id=polygon.id).one().navigator_url in (None, "")
    db.close()


def test_healthz_is_registered_once():
    assert sum(route.path == "/healthz" for route in app_module.app.routes) == 1


def test_bitrix_status_transitions_set_real_stages_for_both_processes(monkeypatch):
    db, admin, driver, vt = reset_db()
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    trips = [
        models.TripRequest(number="П-STAGE", planned_date=date(2026, 8, 12), kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.ACCEPTED, bitrix_element_id=501),
        models.TripRequest(number="С-STAGE", planned_date=date(2026, 8, 12), kind=models.TripType.SAMOSVAL, status=models.RequestStatus.ACCEPTED, bitrix_element_id=502),
    ]
    db.add(settings); db.add_all(trips); db.commit()
    entity_by_kind = {models.TripType.PUKHTOVOZ: "1088", models.TripType.SAMOSVAL: "1092"}
    stage_ids = {
        "Водитель назначен": "DRIVER_ASSIGNED",
        "Рейс начат": "TRIP_STARTED",
        "Рейс завершен": "TRIP_FINISHED",
        "Успех": "SUCCESS",
    }
    calls = []
    monkeypatch.setattr(app_module.bitrix, "resolve_process_entity", lambda url, kind: entity_by_kind[kind])
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {"title": {"title": "Название"}})
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {"id": item, "categoryId": 7})

    def fake_post(url, method, payload):
        calls.append((method, payload))
        if method == "crm.category.list":
            return {"result": {"categories": [{"id": 9, "isDefault": True}]}}
        if method == "crm.status.list":
            _, entity, _, category = payload["filter"]["ENTITY_ID"].split("_")
            return {"result": [
                {"NAME": title, "STATUS_ID": f"DT{entity}_{category}:{suffix}"}
                for title, suffix in stage_ids.items()
            ]}
        if method == "crm.item.update":
            return {"result": {}}
        if method == "crm.item.add":
            return {"result": {"item": {"id": 503}}}
        raise AssertionError(method)

    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)
    transitions = [
        (models.RequestStatus.ACCEPTED, "DRIVER_ASSIGNED"),
        (models.RequestStatus.IN_WORK, "TRIP_STARTED"),
        (models.RequestStatus.DRIVER_COMPLETED, "TRIP_FINISHED"),
        (models.RequestStatus.LOGIST_CONFIRMED, "SUCCESS"),
    ]
    for trip in trips:
        entity = entity_by_kind[trip.kind]
        for status, suffix in transitions:
            trip.status = status
            result = app_module.bitrix.sync_trip(trip, db, settings=settings)
            assert result["ok"] is True
            update = calls[-1]
            assert update[0] == "crm.item.update"
            assert update[1]["entityTypeId"] == int(entity)
            assert update[1]["fields"]["stageId"] == f"DT{entity}_7:{suffix}"
            status_call = calls[-2]
            assert status_call[1]["filter"]["ENTITY_ID"] == f"DYNAMIC_{entity}_STAGE_7"

    new_trip = models.TripRequest(
        number="С-STAGE-ADD", planned_date=date(2026, 8, 12),
        kind=models.TripType.SAMOSVAL, status=models.RequestStatus.ACCEPTED,
    )
    db.add(new_trip); db.commit()
    result = app_module.bitrix.sync_trip(new_trip, db, settings=settings)
    assert result["action"] == "add" and new_trip.bitrix_element_id == 503
    add_call = calls[-1]
    assert add_call[0] == "crm.item.add"
    assert add_call[1]["entityTypeId"] == 1092
    assert add_call[1]["fields"]["categoryId"] == 9
    assert add_call[1]["fields"]["stageId"] == "DT1092_9:DRIVER_ASSIGNED"
    db.close()


def test_bitrix_stage_without_id_fails_closed_without_item_write(monkeypatch):
    db, admin, driver, vt = reset_db()
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    trip = models.TripRequest(
        number="П-STAGE-NO-ID", planned_date=date(2026, 8, 12),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.ACCEPTED,
        bitrix_element_id=504,
    )
    db.add_all([settings, trip]); db.commit()
    writes = []
    monkeypatch.setattr(app_module.bitrix, "resolve_process_entity", lambda url, kind: "1088")
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {"title": {"title": "Название"}})
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {"categoryId": 7})

    def fake_post(url, method, payload):
        if method == "crm.status.list":
            return {"result": [{"NAME": "Водитель назначен"}]}
        writes.append((method, payload))
        return {"result": {}}

    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)
    result = app_module.bitrix.sync_trip(trip, db, settings=settings)
    assert result == {"error": "bitrix_stage_not_found", "action": "stage"}
    assert writes == []
    db.close()


def test_bitrix_can_upsert_local_trip_from_smart_process(monkeypatch):
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто Б24", plate="С333СС78", type_id=vt.id)
    polygon = models.Polygon(name="Полигон Б24")
    cargo = models.CargoType(name="Песок Б24", unit="м³")
    tariff = models.Tariff(
        title="Тариф Б24", vehicle_type_id=vt.id, kind=models.TripType.PUKHTOVOZ,
        trip_price=5000, formula="trip", is_active=True,
    )
    db.add_all([vehicle, polygon, cargo, tariff]); db.commit()
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add(settings); db.commit()

    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"150": models.TripType.PUKHTOVOZ, "151": models.TripType.SAMOSVAL})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {
        "id": 900, "title": "П-Б24-900", "ufReisDate": "2026-08-10T09:30:00",
        "ufDriver": driver.full_name, "ufVehicle": vehicle.name, "ufPolygon": polygon.name,
        "ufVolumePlan": 14, "ufKmPlan": 25, "ufVolumeFact": 0, "ufKmFact": 0, "ufStatus": "Новая",
        "ufTripsCount": 3, "ufCargoType": cargo.name, "ufTariff": tariff.title,
        "ufSumTrip": 15000, "ufSumDriver": 12000, "ufWasteBinCount": 2,
        "ufStartedAt": "2026-08-10T09:45:00", "ufFinishedAt": "2026-08-10T11:30:00",
    })
    result = app_module.bitrix.sync_from_bitrix(900, 150, db, settings=settings)
    db.commit()
    trip = db.query(models.TripRequest).filter(models.TripRequest.bitrix_element_id == 900).one()
    assert result["ok"] is True
    assert trip.number == "П-Б24-900"
    assert trip.kind == models.TripType.PUKHTOVOZ
    assert trip.volume == 14
    assert trip.planned_date == date(2026, 8, 10) and trip.planned_time == "09:30"
    assert trip.actual_volume == 0
    assert trip.actual_km == 0
    assert trip.driver_id == driver.id
    assert trip.vehicle_id == vehicle.id
    assert trip.polygon_id == polygon.id
    assert trip.trips_count == 3
    assert trip.cargo_type_id == cargo.id
    assert trip.tariff_id == tariff.id
    assert trip.sum_trip is None and trip.sum_driver is None
    assert trip.waste_bin_count == 2
    assert trip.started_at == app_module.datetime(2026, 8, 10, 9, 45)
    assert trip.finished_at == app_module.datetime(2026, 8, 10, 11, 30)
    db.close()


def test_bitrix_cannot_override_driver_payment_and_rejects_zero_trip_count(monkeypatch):
    db, admin, driver, vt = reset_db()
    trip = models.TripRequest(
        number="П-PAYMENT-GUARD", planned_date=date(2026, 8, 13), driver_id=driver.id,
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.ASSIGNED,
        km=20, volume=10, trips_count=2, sum_trip=12000, sum_driver=10000,
        bitrix_element_id=501, bitrix_entity_type_id=77,
    )
    db.add(trip); db.commit()
    settings = models.IntegrationSetting(
        provider="bitrix24", is_active=True,
        webhook_url="https://example.test/rest/1/token/",
    )
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda *_: {"77": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda *_: {})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *_: None)

    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *_: {
        "id": 501, "title": "П-PAYMENT-GUARD", "ufSumTrip": 1,
        "ufSumDriver": 999999, "ufStatus": "Неизвестный внешний статус",
    })
    result = app_module.bitrix.sync_from_bitrix(501, 77, db, settings)
    assert result.get("error") is None
    db.refresh(trip)
    assert trip.sum_trip == 12000 and trip.sum_driver == 10000
    assert trip.status == models.RequestStatus.ASSIGNED

    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *_: {
        "id": 501, "title": "П-PAYMENT-GUARD", "ufTripsCount": 0,
    })
    accepted = app_module.bitrix.sync_from_bitrix(501, 77, db, settings)
    assert accepted["ok"] is True
    db.refresh(trip)
    assert trip.trips_count == 2
    db.close()


def test_bitrix_inbound_uses_real_kanban_stage_for_both_processes(monkeypatch):
    db, admin, driver, vt = reset_db()
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True,
    )
    db.add(settings); db.commit()
    monkeypatch.setattr(
        app_module.bitrix,
        "resolve_process_kinds",
        lambda url: {"1088": models.TripType.PUKHTOVOZ, "1092": models.TripType.SAMOSVAL},
    )
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    expected_by_suffix = {
        "DRIVER_ASSIGNED": models.RequestStatus.ACCEPTED,
        "TRIP_STARTED": models.RequestStatus.IN_WORK,
        "TRIP_FINISHED": models.RequestStatus.DRIVER_COMPLETED,
        "SUCCESS": models.RequestStatus.LOGIST_CONFIRMED,
    }
    current = {}

    def fake_item(url, entity, item):
        suffix = current["suffix"]
        return {
            "id": item,
            "title": f"Б24-{entity}-{suffix}",
            "categoryId": 7,
            "stageId": f"DT{entity}_7:{suffix}",
        }

    def fake_post(url, method, payload):
        assert method == "crm.status.list"
        entity = payload["filter"]["ENTITY_ID"].split("_")[1]
        return {"result": [
            {"NAME": title, "STATUS_ID": f"DT{entity}_7:{suffix}"}
            for title, suffix in {
                "Водитель назначен": "DRIVER_ASSIGNED",
                "Рейс начат": "TRIP_STARTED",
                "Рейс завершен": "TRIP_FINISHED",
                "Успех": "SUCCESS",
            }.items()
        ]}

    monkeypatch.setattr(app_module.bitrix, "fetch_item", fake_item)
    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)

    item_id = 1000
    for entity_id in (1088, 1092):
        for suffix, expected_status in expected_by_suffix.items():
            item_id += 1
            current["suffix"] = suffix
            result = app_module.bitrix.sync_from_bitrix(item_id, entity_id, db, settings=settings)
            db.flush()
            trip = db.query(models.TripRequest).filter_by(
                bitrix_element_id=item_id, bitrix_entity_type_id=entity_id,
            ).one()
            assert result["ok"] is True
            assert trip.status == expected_status
    db.close()


def test_bitrix_webhook_updates_existing_trip_from_kanban_stage_without_duplicate(monkeypatch):
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто webhook", plate="В202ВВ78", type_id=vt.id, is_active=True)
    tariff = models.Tariff(
        title="Webhook тариф", vehicle_type_id=vt.id, kind=models.TripType.PUKHTOVOZ,
        trip_price=5000, formula="trip", is_active=True,
    )
    db.add_all([vehicle, tariff]); db.flush()
    trip = models.TripRequest(
        number="П-WEBHOOK", planned_date=date(2026, 8, 12), driver_id=driver.id,
        vehicle_id=vehicle.id, tariff_id=tariff.id, kind=models.TripType.PUKHTOVOZ,
        status=models.RequestStatus.ACCEPTED, bitrix_element_id=777,
        bitrix_entity_type_id=1088, trips_count=1, km=0, volume=0,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="hook-secret", is_active=True,
    )
    db.add_all([trip, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {
        "id": item, "title": trip.number, "categoryId": 7,
        "stageId": "DT1088_7:TRIP_STARTED", "ufDriver": driver.full_name,
        "ufVehicle": vehicle.name, "ufTripsCount": 1, "ufKmPlan": 0, "ufVolumePlan": 0,
    })
    monkeypatch.setattr(app_module.bitrix, "_http_post", lambda url, method, payload: {
        "result": [{"NAME": "Рейс начат", "STATUS_ID": "DT1088_7:TRIP_STARTED"}],
    })

    payload = {
        "event": "ONCRMDYNAMICITEMUPDATE",
        "data[FIELDS][ID]": "777",
        "data[FIELDS][ENTITY_TYPE_ID]": "1088",
    }
    response = TestClient(app_module.app).post("/webhook/bitrix24?token=hook-secret", json=payload)
    assert response.status_code == 200
    db.expire_all()
    saved = db.query(models.TripRequest).filter_by(id=trip.id).one()
    assert saved.status == models.RequestStatus.IN_WORK
    assert db.query(models.TripRequest).filter_by(number=trip.number).count() == 1
    history = db.query(models.StatusHistory).filter_by(trip_request_id=trip.id).all()
    assert len(history) == 1
    assert history[0].old_status == models.RequestStatus.ACCEPTED.value
    assert history[0].new_status == models.RequestStatus.IN_WORK.value
    db.close()


def test_bitrix_partial_update_preserves_fields_missing_from_card(monkeypatch):
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто частичный обмен", plate="Т303ТТ78", type_id=vt.id)
    db.add(vehicle); db.flush()
    trip = models.TripRequest(
        number="П-PARTIAL", planned_date=date(2026, 8, 12), planned_time="15:30",
        driver_id=driver.id, vehicle_id=vehicle.id, kind=models.TripType.PUKHTOVOZ,
        status=models.RequestStatus.ACCEPTED, km=42, volume=18, trips_count=2,
        actual_km=40, actual_volume=17, sum_trip=14000, sum_driver=12000,
        comment="Комментарий приложения", logist_comment="Проверить документы",
        started_at=app_module.datetime(2026, 8, 12, 15, 45), waste_bin_count=3,
        bitrix_element_id=808, bitrix_entity_type_id=1088,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True,
    )
    db.add_all([trip, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {
        "id": item, "title": trip.number, "categoryId": 7, "stageId": "DT1088_7:TRIP_STARTED",
    })
    monkeypatch.setattr(app_module.bitrix, "_http_post", lambda url, method, payload: {
        "result": [{"NAME": "Рейс начат", "STATUS_ID": "DT1088_7:TRIP_STARTED"}],
    })

    result = app_module.bitrix.sync_from_bitrix(808, 1088, db, settings=settings)
    db.flush()
    assert result["ok"] is True
    assert trip.status == models.RequestStatus.IN_WORK
    assert (trip.km, trip.volume, trip.trips_count) == (42, 18, 2)
    assert (trip.actual_km, trip.actual_volume) == (40, 17)
    assert (trip.sum_trip, trip.sum_driver) == (14000, 12000)
    assert trip.comment == "Комментарий приложения"
    assert trip.logist_comment == "Проверить документы"
    assert trip.started_at == app_module.datetime(2026, 8, 12, 15, 45)
    assert trip.waste_bin_count == 3
    db.close()


def test_bitrix_status_probe_requires_admin_and_reports_both_directions():
    app_module.BITRIX_LAST_EVENT = {"received": True, "result": "test-inbound"}
    app_module.BITRIX_LAST_OUTBOUND = {"attempted": True, "result": {"error": "test-outbound"}}
    app_module.app.dependency_overrides.pop(app_module.get_current_user, None)
    assert TestClient(app_module.app).get("/settings/bitrix/status").status_code == 401
    db, admin, *_ = reset_db()
    response = client_as(admin).get("/settings/bitrix/status")
    assert response.status_code == 200
    assert response.json() == {
        "inbound": {"received": True, "result": {}},
        "outbound": {"attempted": True, "result": {"error": "bitrix_sync_error"}},
    }
    db.close()


def test_ground_bitrix_process_ids_route_and_same_item_id_does_not_collide(monkeypatch):
    db, admin, driver, vt = reset_db()
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add(settings); db.commit()

    monkeypatch.setattr(app_module.bitrix, "find_smart_process_ids", lambda url: {"_error": "offline"})
    kinds = app_module.bitrix.resolve_process_kinds(settings.webhook_url)
    assert kinds["1088"] == models.TripType.PUKHTOVOZ
    assert kinds["1092"] == models.TripType.SAMOSVAL

    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {
        "id": item,
        "title": f"{'П' if int(entity) == 1088 else 'С'}-Б24-{item}",
        "ufReisDate": "2026-08-10",
        "ufDriver": driver.full_name,
    })
    first = app_module.bitrix.sync_from_bitrix(4, 1088, db, settings=settings)
    second = app_module.bitrix.sync_from_bitrix(4, 1092, db, settings=settings)
    db.commit()

    trips = db.query(models.TripRequest).filter(models.TripRequest.bitrix_element_id == 4).order_by(models.TripRequest.kind).all()
    assert first["action"] == "add" and second["action"] == "add"
    assert len(trips) == 2
    assert {(t.bitrix_entity_type_id, t.kind) for t in trips} == {
        (1088, models.TripType.PUKHTOVOZ),
        (1092, models.TripType.SAMOSVAL),
    }
    db.close()


def test_polygon_can_store_multiple_cargo_tariffs_with_different_units_and_update_existing():
    db, admin, *_ = reset_db()
    polygon = models.Polygon(name="Полигон тарифный", address="Адрес")
    construction = models.CargoType(name="Строительный мусор", unit="м³")
    industrial = models.CargoType(name="Промышленный мусор", unit="т")
    db.add_all([polygon, construction, industrial]); db.commit()
    client = client_as(admin)

    response = client.post("/polygon-tariffs", data={
        "polygon_id": str(polygon.id),
        "cargo_type_id": [str(construction.id), str(industrial.id)],
        "rate": ["900", "3570"],
        "unit": ["м³", "т"],
        "return_to": "/polygons",
    }, follow_redirects=False)
    assert response.status_code == 302
    rows = db.query(models.PolygonTariff).filter_by(polygon_id=polygon.id).order_by(models.PolygonTariff.cargo_type_id).all()
    assert len(rows) == 2
    assert {(row.cargo_type.name, row.rate, row.unit) for row in rows} == {
        ("Строительный мусор", 900, "м³"),
        ("Промышленный мусор", 3570, "т"),
    }

    updated = client.post("/polygon-tariffs", data={
        "polygon_id": str(polygon.id),
        "cargo_type_id": [str(construction.id)],
        "rate": ["950"],
        "unit": ["м³"],
        "return_to": "/settings#polygons",
    }, follow_redirects=False)
    assert updated.status_code == 302
    db.expire_all()
    assert db.query(models.PolygonTariff).filter_by(polygon_id=polygon.id).count() == 2
    assert db.query(models.PolygonTariff).filter_by(polygon_id=polygon.id, cargo_type_id=construction.id).one().rate == 950
    db.close()


def test_polygon_tariffs_are_visible_in_polygon_settings_and_request_preview():
    db, admin, *_ = reset_db()
    polygon = models.Polygon(name="Полигон Север", address="Север")
    construction = models.CargoType(name="Строительный мусор", unit="м³")
    industrial = models.CargoType(name="Промышленный мусор", unit="т")
    db.add_all([polygon, construction, industrial]); db.flush()
    db.add_all([
        models.PolygonTariff(polygon_id=polygon.id, cargo_type_id=construction.id, rate=900, unit="м³"),
        models.PolygonTariff(polygon_id=polygon.id, cargo_type_id=industrial.id, rate=3570, unit="т"),
    ])
    db.commit()
    client = client_as(admin)

    polygons_page = client.get("/polygons")
    assert polygons_page.status_code == 200
    assert "Добавить или изменить тарифы полигона" in polygons_page.text
    assert "Строительный мусор" in polygons_page.text and "900" in polygons_page.text and "₽/м³" in polygons_page.text
    assert "Промышленный мусор" in polygons_page.text and "3 570" in polygons_page.text and "₽/т" in polygons_page.text

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert "Полигоны и тарифы" in settings_page.text
    assert "settings-section" in settings_page.text
    assert "Сохранить тарифы полигона" in settings_page.text

    request_page = client.get("/pukhtovoz/new")
    assert request_page.status_code == 200
    assert "Тариф полигона" in request_page.text
    assert "Предварительные затраты полигона" in request_page.text
    assert '"rate": 900' in request_page.text
    assert '"rate": 3570' in request_page.text
    assert "syncPolygonTariff" in request_page.text
    db.close()


def test_driver_request_shows_polygon_navigation_and_dispatcher_details():
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто навигация", plate="Н111НН78", type_id=vt.id, is_active=True)
    polygon = models.Polygon(
        name="Полигон Навигация",
        address="Ленинградская область, Полигонная 15",
        contact="Диспетчер Сергей",
        phone="+79995554433",
        navigator_url="https://yandex.ru/maps/?text=polygon",
    )
    db.add_all([vehicle, polygon]); db.flush()
    trip = models.TripRequest(
        number="П-NAV", planned_date=date(2026, 8, 24), driver_id=driver.id,
        vehicle_id=vehicle.id, polygon_id=polygon.id, kind=models.TripType.PUKHTOVOZ,
        status=models.RequestStatus.ASSIGNED,
    )
    db.add(trip); db.commit()

    page = client_as(driver).get(f"/requests/{trip.id}")
    assert page.status_code == 200
    assert "Клиент и объект" in page.text
    assert "Полигон" in page.text
    assert "Рейс и техника" in page.text
    assert "Фактические данные и отметки" in page.text
    assert "Адрес полигона" in page.text
    assert "Ленинградская область, Полигонная 15" in page.text
    assert "Диспетчер Сергей" in page.text
    assert "+79995554433" in page.text
    assert 'href="tel:+79995554433"' in page.text
    assert 'href="https://yandex.ru/maps/?text=polygon"' in page.text
    assert "Открыть в навигаторе" in page.text
    db.close()


def test_admin_day_report_includes_trip_polygon_navigation_and_dispatcher_details():
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто отчёт", plate="О222ОО78", type_id=vt.id, is_active=True)
    polygon = models.Polygon(
        name="Полигон Отчёт",
        address="Москва, Полигонная 10",
        contact="Диспетчер Анна",
        phone="+79990001122",
        navigator_url="https://yandex.ru/maps/?text=report-polygon",
    )
    db.add_all([vehicle, polygon]); db.flush()
    trip = models.TripRequest(
        number="П-DAY", planned_date=date(2026, 8, 24), planned_time="12:30",
        driver_id=driver.id, vehicle_id=vehicle.id, polygon_id=polygon.id,
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.DRIVER_COMPLETED,
    )
    report = models.DriverDayReport(
        report_date=date(2026, 8, 24), driver_id=driver.id, vehicle_id=vehicle.id,
        total_km=120, odometer=45678, fuel_liters=55, comment="Смена завершена",
    )
    db.add_all([trip, report]); db.commit()

    page = client_as(admin).get("/reports/day")
    assert page.status_code == 200
    assert "П-DAY" in page.text
    assert "Полигон Отчёт" in page.text
    assert "Москва, Полигонная 10" in page.text
    assert "Диспетчер Анна" in page.text
    assert "+79990001122" in page.text
    assert 'href="https://yandex.ru/maps/?text=report-polygon"' in page.text
    assert "Открыть в навигаторе" in page.text
    db.close()


def test_request_form_uses_client_navigation_instead_of_unload_address():
    db, admin, driver, vt = reset_db()
    page = client_as(admin).get("/pukhtovoz/new")
    assert page.status_code == 200
    assert "Навигация по объекту (Яндекс Карты)" in page.text
    assert "Адрес выгрузки" not in page.text
    assert 'name="unload_address"' in page.text
    db.close()


def test_driver_request_shows_clickable_client_yandex_navigation():
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="Авто объект", plate="К333КК78", type_id=vt.id, is_active=True)
    db.add(vehicle); db.flush()
    trip = models.TripRequest(
        number="П-OBJ-NAV", planned_date=date(2026, 8, 24), driver_id=driver.id,
        vehicle_id=vehicle.id, kind=models.TripType.PUKHTOVOZ,
        status=models.RequestStatus.ASSIGNED, load_address="Москва, Объектовая 5",
        unload_address="https://yandex.ru/maps/?text=client-object",
    )
    db.add(trip); db.commit()

    page = client_as(driver).get(f"/requests/{trip.id}")
    assert page.status_code == 200
    assert "Навигация по объекту" in page.text
    assert 'href="https://yandex.ru/maps/?text=client-object"' in page.text
    assert "Открыть в Яндекс Картах" in page.text
    db.close()


def test_bitrix_status_uses_persisted_outbound_after_runtime_reset():
    db, admin, *_ = reset_db()
    persisted = {
        "attempted": True,
        "request_id": 321,
        "kind": models.TripType.PUKHTOVOZ.value,
        "result": {"ok": True, "action": "add", "element_id": 654},
    }
    db.add(models.AuditLog(
        action="sync", section="bitrix_outbound", record_id=321,
        new_value=app_module.json.dumps(persisted, ensure_ascii=False),
    ))
    db.commit()
    app_module.BITRIX_LAST_OUTBOUND = {"attempted": False}
    response = client_as(admin).get("/settings/bitrix/status")
    assert response.status_code == 200
    assert response.json()["outbound"] == persisted
    db.close()


def test_bitrix_final_trip_still_pulls_descriptive_fields_but_keeps_final_status(monkeypatch):
    db, admin, driver, vt = reset_db()
    trip = models.TripRequest(
        number="П-FINAL-ECHO", planned_date=date(2026, 8, 12),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.LOGIST_CONFIRMED,
        bitrix_element_id=777, bitrix_entity_type_id=1088, comment="старый комментарий",
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="hook-secret", is_active=True,
    )
    db.add_all([trip, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: models.RequestStatus.ACCEPTED)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 777, "title": "П-FINAL-ECHO", "ufComment": "новый комментарий",
    })
    payload = {
        "event": "ONCRMDYNAMICITEMUPDATE",
        "data[FIELDS][ID]": "777",
        "data[FIELDS][ENTITY_TYPE_ID]": "1088",
    }
    response = TestClient(app_module.app).post("/webhook/bitrix24?token=hook-secret", json=payload)
    assert response.status_code == 200
    db.expire_all()
    saved = db.query(models.TripRequest).filter_by(id=trip.id).one()
    assert saved.status == models.RequestStatus.LOGIST_CONFIRMED
    assert saved.comment == "новый комментарий"
    db.close()


def test_bitrix_stale_stage_does_not_rollback_regular_field_update(monkeypatch):
    db, admin, driver, vt = reset_db()
    trip = models.TripRequest(
        number="П-STALE-STAGE", planned_date=date(2026, 8, 24),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.IN_WORK,
        bitrix_element_id=778, bitrix_entity_type_id=1088, load_address="старый адрес",
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="hook-secret", is_active=True,
    )
    db.add_all([trip, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: models.RequestStatus.ACCEPTED)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 778, "title": "П-STALE-STAGE", "ufLoadAddr": "новый адрес из Bitrix",
    })
    response = TestClient(app_module.app).post(
        "/webhook/bitrix24?token=hook-secret",
        json={
            "event": "ONCRMDYNAMICITEMUPDATE",
            "data[FIELDS][ID]": "778",
            "data[FIELDS][ENTITY_TYPE_ID]": "1088",
        },
    )
    assert response.status_code == 200
    db.expire_all()
    saved = db.query(models.TripRequest).filter_by(id=trip.id).one()
    assert saved.load_address == "новый адрес из Bitrix"
    assert saved.status == models.RequestStatus.IN_WORK
    db.close()

def test_outbound_wrapper_records_all_sync_attempts(monkeypatch):
    db, admin, driver, vt = reset_db()
    trip = models.TripRequest(
        number="П-DIAG", planned_date=date(2026, 8, 12),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.ASSIGNED,
    )
    db.add(trip); db.commit()
    monkeypatch.setattr(app_module.bitrix, "sync_trip", lambda req, db: {"skipped": True, "reason": "process_not_found"})
    result = app_module._sync_trip_outbound(trip, db)
    db.commit()
    assert result == {"skipped": True, "reason": "process_not_found"}
    row = db.query(models.AuditLog).filter_by(section="bitrix_outbound", record_id=trip.id).one()
    saved = app_module.json.loads(row.new_value)
    assert saved["attempted"] is True
    assert saved["result"]["skipped"] is True
    assert saved["result"]["reason"] == "process_not_found"
    db.close()


def test_bitrix_webhook_accepts_nested_json_application_token():
    db, admin, *_ = reset_db()
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="nested-secret", is_active=True,
    )
    db.add(settings); db.commit()
    payload = {
        "event": "PING",
        "auth": {"application_token": "nested-secret"},
    }
    response = TestClient(app_module.app).post("/webhook/bitrix24", json=payload)
    assert response.status_code == 200
    assert response.json()["skipped"] == "unsupported_event"

    denied = TestClient(app_module.app).post(
        "/webhook/bitrix24",
        json={"event": "PING", "auth": {"application_token": "wrong-secret"}},
    )
    assert denied.status_code == 403
    db.close()


def test_samosval_outbound_uses_process_1092(monkeypatch):
    db, admin, *_ = reset_db()
    trip = models.TripRequest(
        number="С-BITRIX-1092", planned_date=date(2026, 8, 24),
        kind=models.TripType.SAMOSVAL, status=models.RequestStatus.ASSIGNED,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="hook-secret", is_active=True,
    )
    db.add_all([trip, settings]); db.commit()

    monkeypatch.setattr(app_module.bitrix, "find_smart_process_ids", lambda url: {})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {"title": {"title": "Название"}})
    calls = []

    def fake_post(url, method, payload):
        calls.append((method, payload))
        return {"result": {"item": {"id": 606}}}

    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)
    result = app_module.bitrix.sync_trip(trip, db, settings=settings)
    db.commit()

    assert result == {"ok": True, "action": "add", "element_id": 606}
    assert calls and calls[0][0] == "crm.item.add"
    assert calls[0][1]["entityTypeId"] == 1092
    assert trip.bitrix_entity_type_id == 1092
    assert trip.bitrix_element_id == 606
    db.close()


def test_driver_can_accept_new_request_when_it_is_already_assigned_to_them():
    db, admin, driver, _ = reset_db()
    trip = models.TripRequest(
        number="П-NEW-ASSIGNED-DRIVER",
        planned_date=date.today(),
        driver_id=driver.id,
        kind=models.TripType.PUKHTOVOZ,
        status=models.RequestStatus.NEW,
    )
    db.add(trip); db.commit(); db.refresh(trip)

    response = client_as(driver).post(f"/requests/{trip.id}/accept", follow_redirects=False)
    assert response.status_code == 302
    db.refresh(trip)
    assert trip.status == models.RequestStatus.ACCEPTED
    history = db.query(models.StatusHistory).filter_by(trip_request_id=trip.id).order_by(models.StatusHistory.id.desc()).first()
    assert history.old_status == models.RequestStatus.NEW.value
    assert history.new_status == models.RequestStatus.ACCEPTED.value
    db.close()


def test_request_form_builds_yandex_navigation_from_address_and_has_suggestions():
    db, admin, *_ = reset_db()
    page = client_as(admin).get("/pukhtovoz/new")
    assert page.status_code == 200
    assert 'id="object_navigation_address"' in page.text
    assert 'id="object_address_suggestions"' in page.text
    assert '<datalist' not in page.text
    assert 'class="address-suggestions"' in page.text
    assert 'address-suggestion-option' in page.text
    assert 'aria-expanded="false"' in page.text
    assert 'type="hidden" id="unload_address" name="unload_address"' in page.text
    assert 'https://yandex.ru/maps/?text=' in page.text
    assert '/api/address-suggest?q=' in page.text
    assert 'Адрес не найден. Добавьте город или область' in page.text
    db.close()


def test_address_suggest_returns_city_context_without_writing_to_db(monkeypatch):
    db, admin, *_ = reset_db()
    monkeypatch.delenv("YANDEX_SUGGEST_API_KEY", raising=False)

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self):
            return json.dumps({
                "features": [{
                    "properties": {
                        "street": "Комсомольская улица",
                        "housenumber": "5А",
                        "city": "Первоуральск",
                        "state": "Свердловская область",
                        "country": "Россия",
                    }
                }]
            }).encode("utf-8")

    def fake_urlopen(request, timeout=0):
        assert "photon.komoot.io/api/" in request.full_url
        assert timeout == 5
        return FakeResponse()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    response = client_as(admin).get("/api/address-suggest?q=Комсомольская%205А")
    assert response.status_code == 200
    assert response.json() == {
        "items": ["Комсомольская улица 5А, Первоуральск, Свердловская область, Россия"]
    }
    db.close()



def test_driver_json_attachment_upload_survives_reverse_proxy(tmp_path, monkeypatch):
    db, admin, driver, _ = reset_db()
    trip = models.TripRequest(
        number="П-JSON-FILE", planned_date=date(2026, 8, 24), driver_id=driver.id,
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.IN_WORK,
    )
    db.add(trip); db.commit(); db.refresh(trip)
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path))
    synced = []
    monkeypatch.setattr(app_module.bitrix, "sync_attachments", lambda req, session: synced.append(req.id) or {"ok": True})
    content = b"%PDF-1.4\ntrip-file"
    response = client_as(driver).post(
        f"/requests/{trip.id}/attachments-json",
        json={"files": [{
            "name": "route.pdf", "content_type": "application/pdf",
            "data": base64.b64encode(content).decode("ascii"),
        }]},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "count": 1}
    attachment = db.query(models.Attachment).filter_by(trip_request_id=trip.id).one()
    assert attachment.filename == "route.pdf"
    assert attachment.content == content
    assert synced == [trip.id]
    db.close()


def test_bitrix_attachment_sync_preserves_old_files_and_uploads_new(monkeypatch):
    db, admin, driver, _ = reset_db()
    trip = models.TripRequest(
        number="П-FILE-SYNC", planned_date=date(2026, 8, 24),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.NEW,
        bitrix_element_id=55, bitrix_entity_type_id=1088,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True,
    )
    db.add_all([trip, settings]); db.flush()
    old = models.Attachment(
        trip_request_id=trip.id, filename="old.pdf", content_type="application/pdf",
        size=8, content=b"%PDF-old", path="", bitrix_file_id=10,
    )
    new = models.Attachment(
        trip_request_id=trip.id, filename="new.pdf", content_type="application/pdf",
        size=8, content=b"%PDF-new", path="",
    )
    db.add_all([old, new]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_entity", lambda url, kind: "1088")
    monkeypatch.setattr(app_module.bitrix, "ensure_attachment_field", lambda url, entity: (
        "ufTripFiles", {"ufTripFiles": {"title": "Файлы рейса", "type": "file"}}, {"ok": True, "already": True}
    ))
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 55, "ufTripFiles": [{"id": 10, "urlMachine": "old"}],
    })
    calls = []
    def fake_post(url, method, payload):
        calls.append((method, payload))
        assert method == "crm.item.update"
        sent = payload["fields"]["ufTripFiles"]
        assert sent[0] == {"id": 10}
        assert sent[1][0] == "new.pdf"
        assert base64.b64decode(sent[1][1]) == b"%PDF-new"
        return {"result": {"item": {"id": 55, "ufTripFiles": [{"id": 10}, {"id": 11}]}}}
    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)
    result = app_module.bitrix.sync_attachments(trip, db, settings=settings)
    db.commit(); db.refresh(new)
    assert result["ok"] is True and result["count"] == 2
    assert new.bitrix_file_id == 11
    assert len(calls) == 1
    db.close()


def test_bitrix_inbound_file_field_adds_file_to_local_request(monkeypatch):
    db, admin, driver, _ = reset_db()
    trip = models.TripRequest(
        number="П-FILE-IN", planned_date=date(2026, 8, 24),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.NEW,
    )
    db.add(trip); db.commit()
    monkeypatch.setattr(app_module.bitrix, "_download_signed_file", lambda url: (
        "from-bitrix.pdf", "application/pdf", b"%PDF-from-bitrix"
    ))
    schema = {"ufTripFiles": {"title": "Файлы рейса", "type": "file"}}
    mapping = {"attachments": "ufTripFiles"}
    result = app_module.bitrix.sync_inbound_attachments(
        {"ufTripFiles": [{"id": 77, "urlMachine": "https://example/file"}]},
        trip, db, schema, mapping,
    )
    db.commit()
    attachment = db.query(models.Attachment).filter_by(trip_request_id=trip.id).one()
    assert result == {"ok": True, "count": 1}
    assert attachment.bitrix_file_id == 77
    assert attachment.filename == "from-bitrix.pdf"
    assert attachment.content == b"%PDF-from-bitrix"
    db.close()


def test_bitrix_outbound_binds_company_and_contact_in_spa_client_field(monkeypatch):
    db, admin, driver, _ = reset_db()
    customer = models.Customer(
        name="ООО Клиент", bitrix_company_id=321, bitrix_contact_id=654,
        contact="Иван Петров", phone="+79990000000",
    )
    trip = models.TripRequest(
        number="П-CLIENT-OUT", planned_date=date(2026, 8, 24), customer=customer,
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.NEW,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True,
    )
    db.add_all([customer, trip, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_entity", lambda url, kind: "1088")
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {
        "title": {"title": "Название"},
        "companyId": {"title": "Компания"},
        "contactIds": {"title": "Контакты"},
    })
    calls = []
    def fake_post(url, method, payload):
        calls.append((method, payload))
        return {"result": {"item": {"id": 88}}}
    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)
    result = app_module.bitrix.sync_trip(trip, db, settings=settings)
    assert result["ok"] is True
    assert calls == [("crm.item.add", calls[0][1])]
    fields = calls[0][1]["fields"]
    assert fields["companyId"] == 321
    assert fields["contactIds"] == [654]
    db.close()


def test_bitrix_client_field_can_be_enabled_for_smart_process(monkeypatch):
    monkeypatch.setattr(app_module.bitrix, "_type_info_by_entity", lambda url, entity: {
        "id": 7, "entityTypeId": entity, "isClientEnabled": "N",
    })
    calls = []
    monkeypatch.setattr(app_module.bitrix, "_http_post", lambda url, method, payload: calls.append((method, payload)) or {"result": {}})
    result = app_module.bitrix.ensure_client_field_enabled("https://example/rest/1/token/", 1088)
    assert result == {"ok": True, "already": False}
    assert calls == [("crm.type.update", {"id": 7, "fields": {"isClientEnabled": "Y"}})]


def test_bitrix_inbound_client_company_and_contact_fill_local_customer(monkeypatch):
    db, admin, driver, _ = reset_db()
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True,
    )
    db.add(settings); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1092": models.TripType.SAMOSVAL})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {"title": {"title": "Название"}})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    def fake_fetch(url, entity, item):
        if int(entity) == 1092:
            return {"id": 6, "title": "С-CLIENT-IN", "companyId": 321, "contactIds": [654]}
        if int(entity) == 4:
            return {
                "id": 321, "title": "ООО Заказчик", "address": "Екатеринбург, Ленина 1",
                "fm": [{"typeId": "PHONE", "valueType": "WORK", "value": "+73430000000"}],
            }
        if int(entity) == 3:
            return {
                "id": 654, "name": "Иван", "lastName": "Петров",
                "fm": [{"typeId": "PHONE", "valueType": "MOBILE", "value": "+79990000000"}],
            }
        return {"_error": "unexpected"}
    monkeypatch.setattr(app_module.bitrix, "fetch_item", fake_fetch)
    result = app_module.bitrix.sync_from_bitrix(6, 1092, db, settings=settings)
    db.commit()
    trip = db.query(models.TripRequest).filter_by(bitrix_element_id=6, bitrix_entity_type_id=1092).one()
    customer = db.query(models.Customer).filter_by(id=trip.customer_id).one()
    assert result["ok"] is True
    assert customer.name == "ООО Заказчик"
    assert customer.bitrix_company_id == 321
    assert customer.bitrix_contact_id == 654
    assert customer.contact == "Петров Иван"
    assert customer.phone == "+79990000000"
    assert customer.address == "Екатеринбург, Ленина 1"
    db.close()


def test_bitrix_company_update_event_refreshes_linked_local_customer(monkeypatch):
    db, admin, *_ = reset_db()
    customer = models.Customer(
        name="ООО Старое", address="старый адрес", phone="+70000000000", bitrix_company_id=321,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="hook-secret", is_active=True,
    )
    db.add_all([customer, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {
        "id": 321, "title": "ООО Новое", "address": "Екатеринбург, Ленина 10",
        "fm": [{"typeId": "PHONE", "valueType": "WORK", "value": "+73431234567"}],
    })
    response = TestClient(app_module.app).post(
        "/webhook/bitrix24?token=hook-secret",
        json={"event": "ONCRMCOMPANYUPDATE", "data": {"FIELDS": {"ID": "321"}}},
    )
    assert response.status_code == 200
    db.expire_all()
    saved = db.query(models.Customer).filter_by(id=customer.id).one()
    assert saved.name == "ООО Новое"
    assert saved.address == "Екатеринбург, Ленина 10"
    assert saved.phone == "+73431234567"
    db.close()


def test_bitrix_contact_update_event_refreshes_linked_local_customer(monkeypatch):
    db, admin, *_ = reset_db()
    customer = models.Customer(
        name="ООО Клиент", contact="Старый контакт", phone="+70000000000", bitrix_contact_id=654,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="hook-secret", is_active=True,
    )
    db.add_all([customer, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {
        "id": 654, "name": "Иван", "lastName": "Петров",
        "fm": [{"typeId": "PHONE", "valueType": "MOBILE", "value": "+79995554433"}],
    })
    response = TestClient(app_module.app).post(
        "/webhook/bitrix24?token=hook-secret",
        json={"event": "ONCRMCONTACTUPDATE", "data": {"FIELDS": {"ID": "654"}}},
    )
    assert response.status_code == 200
    db.expire_all()
    saved = db.query(models.Customer).filter_by(id=customer.id).one()
    assert saved.contact == "Петров Иван"
    assert saved.phone == "+79995554433"
    db.close()


def test_bitrix_dynamic_event_with_entity_suffix_is_accepted(monkeypatch):
    db, admin, *_ = reset_db()
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="hook-secret", is_active=True,
    )
    db.add(settings); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 900, "title": "П-SUFFIX",
    })
    response = TestClient(app_module.app).post(
        "/webhook/bitrix24?token=hook-secret",
        json={
            "event": "ONCRMDYNAMICITEMUPDATE_1088",
            "data": {"FIELDS": {"ID": "900", "ENTITY_TYPE_ID": "1088"}},
        },
    )
    assert response.status_code == 200
    assert db.query(models.TripRequest).filter_by(bitrix_element_id=900, bitrix_entity_type_id=1088).one()
    db.close()


def test_bitrix_dynamic_event_suffix_supplies_entity_id_when_payload_omits_it(monkeypatch):
    event, item_id, entity_id = app_module.bitrix.extract_event_identifiers({
        "event": "ONCRMDYNAMICITEMADD_1092",
        "data": {"FIELDS": {"ID": "77"}},
    })
    assert (event, item_id, entity_id) == ("ONCRMDYNAMICITEMADD", 77, 1092)


def test_bitrix_dynamic_event_accepts_lowercase_json_shape():
    event, item_id, entity_id = app_module.bitrix.extract_event_identifiers({
        "event": "ONCRMDYNAMICITEMUPDATE",
        "data": {"fields": {"id": "88", "entityTypeId": "1088"}},
    })
    assert (event, item_id, entity_id) == ("ONCRMDYNAMICITEMUPDATE", 88, 1088)


def test_bitrix_reconcile_pulls_recent_items_for_both_trip_processes(monkeypatch):
    db, admin, *_ = reset_db()
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True,
    )
    db.add(settings); db.commit()
    monkeypatch.setattr(
        app_module.bitrix, "list_recent_trip_ids",
        lambda url, entity_type_id, limit=10: [1] if int(entity_type_id) == 1088 else [2],
    )
    calls = []
    def fake_sync(item_id, entity_type_id, session, settings=None):
        calls.append((item_id, entity_type_id))
        return {"ok": True, "action": "add", "trip_id": item_id}
    monkeypatch.setattr(app_module.bitrix, "sync_from_bitrix", fake_sync)
    result = app_module.bitrix.pull_recent_trips(db, settings=settings, limit_per_process=10)
    assert result == {"ok": True, "checked": 2, "synced": 2, "errors": 0}
    assert calls == [(1, 1088), (2, 1092)]
    db.close()


def test_bitrix_new_remote_item_with_duplicate_title_creates_separate_local_trip(monkeypatch):
    db, admin, *_ = reset_db()
    existing = models.TripRequest(
        number="Повторяющийся рейс", planned_date=date(2026, 8, 25),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.NEW,
        bitrix_element_id=10, bitrix_entity_type_id=1088,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True,
    )
    db.add_all([existing, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda *args: {})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 11, "title": "Повторяющийся рейс",
    })

    result = app_module.bitrix.sync_from_bitrix(11, 1088, db, settings=settings)
    db.commit()

    assert result["ok"] is True and result["action"] == "add"
    rows = db.query(models.TripRequest).order_by(models.TripRequest.id).all()
    assert len(rows) == 2
    assert rows[0].bitrix_element_id == 10
    assert rows[1].bitrix_element_id == 11
    assert rows[1].number.startswith("Повторяющийся рейс [Б24-1088-11]")
    db.close()


def test_bitrix_delete_event_removes_unsalaried_local_trip():
    db, admin, driver, vt = reset_db()
    trip = models.TripRequest(
        number="П-DELETE-FROM-B24", planned_date=date(2026, 8, 25),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.LOGIST_CONFIRMED,
        bitrix_element_id=777, bitrix_entity_type_id=1088,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="delete-secret", is_active=True,
    )
    db.add_all([trip, settings]); db.commit()

    response = TestClient(app_module.app).post(
        "/webhook/bitrix24?token=delete-secret",
        json={"event": "ONCRMDYNAMICITEMDELETE_1088", "data": {"FIELDS": {"ID": "777"}}},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "delete"
    assert db.query(models.TripRequest).filter_by(id=trip.id).first() is None
    db.close()


def test_bitrix_delete_event_keeps_salary_linked_trip_as_history():
    db, admin, driver, vt = reset_db()
    trip = models.TripRequest(
        number="П-DELETE-SALARY", planned_date=date(2026, 8, 25),
        driver_id=driver.id, kind=models.TripType.PUKHTOVOZ,
        status=models.RequestStatus.LOGIST_CONFIRMED,
        bitrix_element_id=778, bitrix_entity_type_id=1088,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/",
        secret="salary-delete-secret", is_active=True,
    )
    db.add_all([trip, settings]); db.flush()
    calc = models.SalaryCalc(
        driver_id=driver.id, date_from=date(2026, 8, 1), date_to=date(2026, 8, 31),
        status=models.CalcStatus.DRAFT,
    )
    db.add(calc); db.flush()
    db.add(models.SalaryCalcItem(salary_calc_id=calc.id, trip_request_id=trip.id, sum=0))
    db.commit()

    response = TestClient(app_module.app).post(
        "/webhook/bitrix24?token=salary-delete-secret",
        json={"event": "ONCRMDYNAMICITEMDELETE_1088", "data": {"FIELDS": {"ID": "778"}}},
    )

    assert response.status_code == 200
    db.expire_all()
    saved = db.query(models.TripRequest).filter_by(id=trip.id).one()
    assert saved.status == models.RequestStatus.CANCELLED
    db.close()


def test_bitrix_client_link_removal_clears_customer_from_trip(monkeypatch):
    db, admin, *_ = reset_db()
    customer = models.Customer(name="ООО Клиент", bitrix_company_id=321)
    trip = models.TripRequest(
        number="П-CLIENT-CLEAR", planned_date=date(2026, 8, 24),
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.NEW,
        bitrix_element_id=901, bitrix_entity_type_id=1088, customer=customer,
    )
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add_all([customer, trip, settings]); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {
        "title": {"title": "Название"}, "companyId": {"title": "Компания"}, "contactIds": {"title": "Контакты"},
    })
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 901, "title": "П-CLIENT-CLEAR", "companyId": 0, "contactIds": [],
    })
    result = app_module.bitrix.sync_from_bitrix(901, 1088, db, settings=settings)
    db.commit(); db.refresh(trip)
    assert result["ok"] is True
    assert trip.customer_id is None
    db.close()


def test_bitrix_field_mapping_does_not_treat_system_company_id_as_customer_name():
    mapping = app_module.bitrix.resolve_field_map({
        "title": {"title": "Название"},
        "companyId": {"title": "Компания"},
        "contactIds": {"title": "Контакты"},
    })
    assert mapping.get("customer_name") is None


def test_request_form_uses_one_datetime_field_and_shows_customer_inn_lookup():
    db, admin, *_ = reset_db()
    customer = models.Customer(name="ООО Тест ИНН", inn="6671234567")
    db.add(customer); db.commit()
    page = client_as(admin).get("/pukhtovoz/new")
    assert page.status_code == 200
    assert 'type="datetime-local" id="planned_at" name="planned_at"' in page.text
    assert 'name="planned_date"' not in page.text
    assert 'name="planned_time"' not in page.text
    assert 'id="customer_inn_lookup"' in page.text
    assert 'ИНН 6671234567' in page.text
    assert '/api/company-suggest?q=' in page.text
    db.close()


def test_company_suggest_uses_dadata_and_returns_inn_company(monkeypatch):
    db, admin, *_ = reset_db()
    db.add(models.IntegrationSetting(provider="dadata", secret="dadata-test", is_active=True))
    db.commit()

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): return False
        def read(self):
            return json.dumps({"suggestions": [{
                "value": 'ООО "РОМАШКА"',
                "data": {"inn": "6671234567", "address": {"value": "г Екатеринбург, ул Тестовая, д 1"}},
            }]}).encode("utf-8")

    def fake_urlopen(request, timeout=0):
        assert "suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party" in request.full_url
        assert request.headers.get("Authorization") == "Token dadata-test"
        assert timeout == 7
        return FakeResponse()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    response = client_as(admin).get("/api/company-suggest?q=6671234567")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["name"] == 'ООО "РОМАШКА"'
    assert item["inn"] == "6671234567"
    assert "Екатеринбург" in item["address"]
    db.close()


def test_address_suggest_prefers_dadata_when_configured(monkeypatch):
    db, admin, *_ = reset_db()
    db.add(models.IntegrationSetting(provider="dadata", secret="dadata-address", is_active=True))
    db.commit()

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): return False
        def read(self):
            return json.dumps({"suggestions": [{"value": "Свердловская обл, г Первоуральск, ул Комсомольская, д 5А"}]}).encode("utf-8")

    def fake_urlopen(request, timeout=0):
        assert "suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address" in request.full_url
        assert timeout == 7
        return FakeResponse()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    response = client_as(admin).get("/api/address-suggest?q=Первоуральск%20Комсомольская%205А")
    assert response.status_code == 200
    assert response.json()["items"] == ["Свердловская обл, г Первоуральск, ул Комсомольская, д 5А"]
    assert response.json()["provider"] == "dadata"
    db.close()


def test_bitrix_company_inn_is_read_from_requisite_and_combined_datetime(monkeypatch):
    db, admin, *_ = reset_db()
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add(settings); db.commit()
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: {})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda url, entity, item: {
        "id": 777, "title": "П-ИНН-777", "ufReisDate": "2026-08-25T14:40:00", "companyId": 321,
    } if int(entity) == 1088 else {"id": 321, "title": "ООО Битрикс Клиент"})

    original_post = app_module.bitrix._http_post
    def fake_post(url, method, payload):
        if method == "crm.requisite.list":
            return {"result": [{"ID": "10", "ENTITY_ID": "321", "RQ_INN": "6671234567"}]}
        return original_post(url, method, payload)
    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)

    result = app_module.bitrix.sync_from_bitrix(777, 1088, db, settings=settings)
    db.commit()
    trip = db.query(models.TripRequest).filter_by(bitrix_element_id=777).one()
    assert result["ok"] is True
    assert trip.planned_date == date(2026, 8, 25) and trip.planned_time == "14:40"
    assert trip.customer is not None and trip.customer.name == "ООО Битрикс Клиент"
    assert trip.customer.inn == "6671234567"
    db.close()


def test_bitrix_inbound_resolves_driver_vehicle_polygon_tariff_and_clean_address(monkeypatch):
    db, admin, driver, vt = reset_db()
    driver.full_name = "Иванов Иван"
    vehicle = models.Vehicle(name="КамАЗ 65115", plate="А123АА196", type_id=vt.id)
    polygon = models.Polygon(name="Широкореченский")
    tariff = models.Tariff(
        title="Основной тариф", vehicle_type_id=vt.id, kind=models.TripType.PUKHTOVOZ,
        trip_price=5000, formula="trip", is_active=True,
    )
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add_all([vehicle, polygon, tariff, settings]); db.commit()

    trip_schema = {
        "title": {"title": "Название", "type": "string"},
        "ufWhen": {"title": "Дата и время", "type": "datetime"},
        "ufAddr": {"title": "Адрес подачи", "type": "address"},
        "ufDriverReal": {"title": "Водитель", "type": "user"},
        "ufVehicleReal": {"title": "Машины", "type": "crm", "settings": {"DYNAMIC_1048": "Y"}},
        "ufPolygonReal": {"title": "Полигон", "type": "enumeration", "items": [{"ID": "10", "VALUE": "Широкореченский"}]},
        "ufTariffReal": {"title": "Тариф", "type": "enumeration", "items": [{"ID": "20", "VALUE": "Основной тариф"}]},
        "ufTripsReal": {"title": "Количество рейсов", "type": "integer"},
        "ufKmReal": {"title": "Километраж", "type": "double"},
        "ufVolumeReal": {"title": "Объём", "type": "double"},
        "ufTonnageReal": {"title": "Тоннаж", "type": "double"},
        "ufCommentReal": {"title": "Комментарий логиста", "type": "string"},
    }
    vehicle_schema = {
        "title": {"title": "Название", "type": "string"},
        "ufPlate": {"title": "Госномер", "type": "string"},
    }

    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: vehicle_schema if int(entity) == 1048 else trip_schema)

    def fake_fetch(url, entity, item_id):
        if int(entity) == 1048:
            return {"id": 55, "title": "Машина 55", "ufPlate": "А123АА196"}
        return {
            "id": 901, "title": "РЕЙС-901",
            "ufWhen": "2026-08-31T07:45:00+05:00",
            "ufAddr": {"id": "987654", "address": "г Первоуральск, ул Комсомольская, д 5А"},
            "ufDriverReal": "77",
            "ufVehicleReal": "T418_55",
            "ufPolygonReal": "10",
            "ufTariffReal": "20",
            "ufTripsReal": 4,
            "ufKmReal": 32.5,
            "ufVolumeReal": 18,
            "ufTonnageReal": 12.7,
            "ufCommentReal": "Заехать через вторые ворота",
        }
    monkeypatch.setattr(app_module.bitrix, "fetch_item", fake_fetch)

    def fake_post(url, method, payload):
        if method == "user.get":
            return {"result": [{"ID": "77", "LAST_NAME": "Иванов", "NAME": "Иван"}]}
        return {"result": {}}
    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)

    result = app_module.bitrix.sync_from_bitrix(901, 1088, db, settings=settings)
    db.commit()
    trip = db.query(models.TripRequest).filter_by(bitrix_element_id=901, bitrix_entity_type_id=1088).one()
    assert result["ok"] is True
    assert trip.planned_date == date(2026, 8, 31) and trip.planned_time == "07:45"
    assert trip.load_address == "г Первоуральск, ул Комсомольская, д 5А"
    assert trip.driver_id == driver.id
    assert trip.vehicle_id == vehicle.id
    assert trip.polygon_id == polygon.id
    assert trip.tariff_id == tariff.id
    assert trip.trips_count == 4
    assert trip.km == 32.5 and trip.volume == 18 and trip.tonnage == 12.7
    assert trip.logist_comment == "Заехать через вторые ворота"
    db.close()


def test_driver_day_report_price_and_fuel_cost_are_sent_to_bitrix(monkeypatch):
    db, admin, driver, vt = reset_db()
    vehicle = models.Vehicle(name="КамАЗ", plate="В555ВВ196", type_id=vt.id)
    trip = models.TripRequest(
        number="РЕЙС-ТОПЛИВО", planned_date=date(2026, 8, 31), driver=driver, vehicle=vehicle,
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.NEW,
        bitrix_element_id=123, bitrix_entity_type_id=1088, polygon_cost_manual=2400, sum_driver=3500,
    )
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", secret="s", is_active=True)
    db.add_all([vehicle, trip, settings]); db.commit()

    schema = {
        "title": {"title": "Название", "type": "string"},
        "ufOdo": {"title": "Показания спидометра", "type": "double"},
        "ufFuel": {"title": "Залито топлива", "type": "double"},
        "ufFuelPrice": {"title": "Цена за литр топлива", "type": "double"},
        "ufFuelCost": {"title": "Затраты на топливо", "type": "double"},
        "ufPolygonCost": {"title": "Затраты на полигон", "type": "double"},
        "ufSalary": {"title": "Зарплата водителя", "type": "double"},
    }
    writes = []
    monkeypatch.setattr(app_module.bitrix, "resolve_process_entity", lambda url, kind: "1088")
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: schema)
    def fake_post(url, method, payload):
        if method == "crm.item.update":
            writes.append(payload)
            return {"result": {"item": {"id": 123}}}
        return {"result": {}}
    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)

    response = client_as(driver).post("/driver/day-report", data={
        "report_date": "2026-08-31", "vehicle_id": str(vehicle.id), "total_km": "120",
        "odometer": "456789", "fuel_liters": "80", "fuel_price": "63.50", "comment": "Смена закрыта",
    }, follow_redirects=False)
    assert response.status_code == 302
    report = db.query(models.DriverDayReport).filter_by(driver_id=driver.id, report_date=date(2026, 8, 31)).one()
    assert report.fuel_price == 63.5
    assert report.fuel_cost == 5080
    assert writes
    fields = writes[-1]["fields"]
    assert fields["ufOdo"] == 456789
    assert fields["ufFuel"] == 80
    assert fields["ufFuelPrice"] == 63.5
    assert fields["ufFuelCost"] == 5080
    assert fields["ufPolygonCost"] == 2400
    assert fields["ufSalary"] == 3500
    db.close()


def test_bitrix_can_update_driver_day_report_fuel_fields(monkeypatch):
    db, admin, driver, vt = reset_db()
    driver.full_name = "Петров Пётр"
    vehicle = models.Vehicle(name="Самосвал", plate="С777СС196", type_id=vt.id)
    settings = models.IntegrationSetting(provider="bitrix24", webhook_url="https://example/rest/1/token/", is_active=True)
    db.add_all([vehicle, settings]); db.commit()
    schema = {
        "title": {"title": "Название"},
        "ufWhen": {"title": "Дата и время", "type": "datetime"},
        "ufDriver": {"title": "Водитель", "type": "string"},
        "ufVehicle": {"title": "Машины", "type": "string"},
        "ufOdo": {"title": "Показания спидометра", "type": "double"},
        "ufFuel": {"title": "Залито топлива", "type": "double"},
        "ufFuelPrice": {"title": "Цена за литр топлива", "type": "double"},
    }
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: schema)
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "fetch_item", lambda *args: {
        "id": 333, "title": "РЕЙС-333", "ufWhen": "2026-08-31T09:10:00",
        "ufDriver": "Петров", "ufVehicle": "С777СС196", "ufOdo": 100500,
        "ufFuel": 50, "ufFuelPrice": 64.2,
    })
    result = app_module.bitrix.sync_from_bitrix(333, 1088, db, settings=settings)
    db.commit()
    trip = db.query(models.TripRequest).filter_by(bitrix_element_id=333).one()
    report = db.query(models.DriverDayReport).filter_by(driver_id=driver.id, report_date=date(2026, 8, 31)).one()
    assert result["ok"] is True
    assert trip.driver_id == driver.id and trip.vehicle_id == vehicle.id
    assert report.odometer == 100500
    assert report.fuel_liters == 50
    assert report.fuel_price == 64.2
    assert round(report.fuel_cost, 2) == 3210.0
    db.close()


def test_bitrix_prefers_real_named_fields_over_stale_legacy_codes():
    schema = {
        "title": {"title": "Название", "type": "string"},
        # Старые технические поля остались в процессе, но реально логист их не заполняет.
        "ufReisDate": {"title": "Старая дата интеграции", "type": "datetime"},
        "ufDriver": {"title": "Старый водитель интеграции", "type": "string"},
        "ufLoadAddr": {"title": "Старый адрес интеграции", "type": "string"},
        "ufTariff": {"title": "Старый тариф интеграции", "type": "string"},
        "ufCustomer": {"title": "Старый заказчик интеграции", "type": "string"},
        # Это реальные поля карточки Bitrix24.
        "ufRealWhen": {"title": "Подача машины", "type": "datetime"},
        "ufRealAddress": {"title": "Адрес подачи", "type": "address"},
        "ufRealDriver": {"title": "Водитель", "type": "integer"},
        "ufRealTariff": {"title": "Тариф", "type": "enumeration"},
        "ufRealCompany": {"title": "Компания", "type": "crm_company"},
    }
    mapping = app_module.bitrix.resolve_field_map(schema)
    assert mapping["planned_at"] == "ufRealWhen"
    assert mapping["load_address"] == "ufRealAddress"
    assert mapping["driver_name"] == "ufRealDriver"
    assert mapping["tariff_name"] == "ufRealTariff"
    assert mapping["customer_name"] == "ufRealCompany"


def test_bitrix_generic_date_alias_never_selects_creation_date():
    schema = {
        "createdTime": {"title": "Дата создания", "type": "datetime"},
        "updatedTime": {"title": "Дата изменения", "type": "datetime"},
        "ufWhen": {"title": "Подача машины", "type": "datetime"},
    }
    mapping = app_module.bitrix.resolve_field_map(schema)
    assert mapping["planned_at"] == "ufWhen"


def test_bitrix_inbound_uses_company_address_time_driver_and_tariff_from_real_fields(monkeypatch):
    db, admin, driver, vt = reset_db()
    driver.full_name = "Иванов Иван"
    tariff = models.Tariff(
        title="Основной тариф", vehicle_type_id=vt.id, kind=models.TripType.PUKHTOVOZ,
        trip_price=5000, formula="trip", is_active=True,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", secret="s", is_active=True,
    )
    db.add_all([tariff, settings]); db.commit()

    schema = {
        "title": {"title": "Название", "type": "string"},
        "createdTime": {"title": "Дата создания", "type": "datetime"},
        "ufReisDate": {"title": "Старая дата интеграции", "type": "datetime"},
        "ufDriver": {"title": "Старый водитель интеграции", "type": "string"},
        "ufLoadAddr": {"title": "Старый адрес интеграции", "type": "string"},
        "ufTariff": {"title": "Старый тариф интеграции", "type": "string"},
        "ufCustomer": {"title": "Старый заказчик интеграции", "type": "string"},
        "ufWhen": {"title": "Подача машины", "type": "datetime"},
        "ufAddress": {"title": "Адрес подачи", "type": "address"},
        "ufDriverReal": {"title": "Водитель", "type": "integer"},
        "ufTariffReal": {"title": "Тариф", "type": "enumeration", "items": [{"ID": "9", "VALUE": "Основной тариф"}]},
        "ufCompanyReal": {"title": "Компания", "type": "crm_company"},
    }
    monkeypatch.setattr(app_module.bitrix, "resolve_process_kinds", lambda url: {"1088": models.TripType.PUKHTOVOZ})
    monkeypatch.setattr(app_module.bitrix, "status_from_stage", lambda *args: None)
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda url, entity: schema)

    def fake_fetch(url, entity, item_id):
        if int(entity) == 4:
            return {"id": 321, "title": "ООО Ромашка", "address": "г Екатеринбург, ул Мира, 1"}
        return {
            "id": 777,
            "title": "РЕЙС-777",
            "createdTime": "2026-08-30T23:55:00+05:00",
            "ufReisDate": "2020-01-01T00:00:00+05:00",
            "ufDriver": "",
            "ufLoadAddr": "",
            "ufTariff": "",
            "ufCustomer": "",
            "ufWhen": "2026-08-31T09:40:00Z",
            "ufAddress": "777888|г Первоуральск, ул Комсомольская, д 5А",
            "ufDriverReal": "77",
            "ufTariffReal": "9",
            "ufCompanyReal": "321",
        }
    monkeypatch.setattr(app_module.bitrix, "fetch_item", fake_fetch)

    def fake_post(url, method, payload):
        if method == "server.time":
            return {"result": "2026-08-31T14:40:00+05:00"}
        if method == "user.get":
            return {"result": [{"ID": "77", "LAST_NAME": "Иванов", "NAME": "Иван"}]}
        if method == "crm.requisite.list":
            return {"result": []}
        return {"result": {}}
    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)

    result = app_module.bitrix.sync_from_bitrix(777, 1088, db, settings=settings)
    db.commit()
    trip = db.query(models.TripRequest).filter_by(bitrix_element_id=777, bitrix_entity_type_id=1088).one()
    assert result["ok"] is True
    assert trip.customer is not None and trip.customer.name == "ООО Ромашка"
    assert trip.load_address == "г Первоуральск, ул Комсомольская, д 5А"
    # 09:40 UTC должно отображаться как 14:40 в часовом поясе портала +05.
    assert trip.planned_date == date(2026, 8, 31)
    assert trip.planned_time == "14:40"
    assert trip.driver_id == driver.id
    assert trip.tariff_id == tariff.id
    db.close()


def test_bitrix_outbound_writes_real_named_fields_and_custom_company(monkeypatch):
    db, admin, driver, vt = reset_db()
    driver.full_name = "Иванов Иван"
    customer = models.Customer(name="ООО Ромашка", bitrix_company_id=321)
    trip = models.TripRequest(
        number="РЕЙС-OUT-1", planned_date=date(2026, 8, 31), planned_time="14:40",
        driver=driver, customer=customer, load_address="г Первоуральск, ул Комсомольская, д 5А",
        kind=models.TripType.PUKHTOVOZ, status=models.RequestStatus.NEW,
    )
    settings = models.IntegrationSetting(
        provider="bitrix24", webhook_url="https://example/rest/1/token/", secret="s", is_active=True,
    )
    db.add_all([customer, trip, settings]); db.commit()
    schema = {
        "title": {"title": "Название", "type": "string"},
        "ufReisDate": {"title": "Старая дата интеграции", "type": "datetime"},
        "ufDriver": {"title": "Старый водитель интеграции", "type": "string"},
        "ufLoadAddr": {"title": "Старый адрес интеграции", "type": "string"},
        "ufCustomer": {"title": "Старый заказчик интеграции", "type": "string"},
        "ufWhen": {"title": "Подача машины", "type": "datetime"},
        "ufAddress": {"title": "Адрес подачи", "type": "string"},
        "ufDriverReal": {"title": "Водитель", "type": "integer"},
        "ufCompanyReal": {"title": "Компания", "type": "crm_company"},
    }
    monkeypatch.setattr(app_module.bitrix, "resolve_process_entity", lambda *args: "1088")
    monkeypatch.setattr(app_module.bitrix, "get_element_fields", lambda *args: schema)
    monkeypatch.setattr(app_module.bitrix, "resolve_stage", lambda *args: ("DT1088_1:NEW", 1))
    monkeypatch.setattr(app_module.bitrix, "_ensure_bitrix_customer", lambda *args: (321, None))
    writes = []
    def fake_post(url, method, payload):
        if method == "server.time":
            return {"result": "2026-08-31T14:00:00+05:00"}
        if method == "user.get":
            return {"result": [{"ID": "77", "LAST_NAME": "Иванов", "NAME": "Иван"}]}
        if method == "crm.item.add":
            writes.append(payload)
            return {"result": {"item": {"id": 999}}}
        return {"result": {}}
    monkeypatch.setattr(app_module.bitrix, "_http_post", fake_post)
    result = app_module.bitrix.sync_trip(trip, db, settings=settings)
    assert result["ok"] is True
    fields = writes[-1]["fields"]
    assert fields["ufWhen"].startswith("2026-08-31T14:40")
    assert fields["ufAddress"] == "г Первоуральск, ул Комсомольская, д 5А"
    assert fields["ufDriverReal"] == 77
    assert fields["ufCompanyReal"] == 321
    assert "ufReisDate" not in fields
    assert "ufDriver" not in fields
    assert "ufLoadAddr" not in fields
    assert "ufCustomer" not in fields
    db.close()

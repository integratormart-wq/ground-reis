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


def test_bitrix_customer_identity_rejects_normalized_name_conflict(monkeypatch):
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
    assert result == {"error": "customer_identity_conflict"}
    db.rollback(); db.expire_all()
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
        "id": 900, "title": "П-Б24-900", "ufReisDate": "2026-08-10", "ufReisTime": "09:30",
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
    rejected = app_module.bitrix.sync_from_bitrix(501, 77, db, settings)
    assert rejected == {"error": "invalid trips_count"}
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

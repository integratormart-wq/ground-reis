import os
from datetime import date

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_tariff_forms.db")
os.environ.setdefault("SECRET_KEY", "test-tariff-forms-secret")

from fastapi.testclient import TestClient

import app as app_module
from backend import models


def _setup():
    models.Base.metadata.drop_all(bind=models.engine)
    models.Base.metadata.create_all(bind=models.engine)
    db = models.SessionLocal()
    admin = models.User(
        full_name="Администратор",
        login="tariff-admin",
        password_hash=app_module.pwd_hash("pass"),
        role=models.UserRole.ADMIN,
        is_active=True,
    )
    pukhtovoz_type = models.VehicleType(name="Пухтовоз", kind=models.TripType.PUKHTOVOZ)
    samosval_type = models.VehicleType(name="Самосвал", kind=models.TripType.SAMOSVAL)
    db.add_all([admin, pukhtovoz_type, samosval_type])
    db.commit()
    for row in (admin, pukhtovoz_type, samosval_type):
        db.refresh(row)
    app_module.app.dependency_overrides[app_module.get_current_user] = lambda: admin
    client = TestClient(app_module.app, headers={"Origin": "http://testserver"})
    return db, client, pukhtovoz_type, samosval_type


def _form(html, form_id):
    return html.split(f'id="{form_id}"', 1)[1].split("</form>", 1)[0]


def test_tariff_forms_have_only_requested_fields():
    db, client, _, _ = _setup()
    response = client.get("/settings")
    assert response.status_code == 200

    pukhtovoz = _form(response.text, "pukhtovoz-tariff-form")
    assert "Название тарифа" in pukhtovoz
    assert "Стоимость рейса, ₽" in pukhtovoz
    for removed in ("Расстояние", "Объём", "Тип автомобиля", "Формула", "Коэффициент", "Доплата"):
        assert removed not in pukhtovoz

    samosval = _form(response.text, "samosval-tariff-form")
    for required in ("Название тарифа", "Объём, м³", "Километры", "Стоимость рейса, ₽"):
        assert required in samosval
    for removed in ("Тип автомобиля", "Формула", "Коэффициент", "Доплата", "Минимум", "Максимум"):
        assert removed not in samosval
    db.close()


def test_pukhtovoz_grid_saves_title_and_trip_price_only():
    db, client, pukhtovoz_type, _ = _setup()
    response = client.post(
        "/settings/tariffs/pukhtovoz-grid",
        data={
            "pukhtovoz_title": ["Обычный", "Срочный"],
            "pukhtovoz_price": ["3500", "4800"],
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    rows = db.query(models.Tariff).order_by(models.Tariff.id).all()
    assert [(row.title, row.trip_price) for row in rows] == [("Обычный", 3500), ("Срочный", 4800)]
    assert all(row.kind == models.TripType.PUKHTOVOZ for row in rows)
    assert all(row.vehicle_type_id == pukhtovoz_type.id for row in rows)
    assert all((row.min_km, row.max_km, row.min_volume, row.max_volume) == (0, None, 0, None) for row in rows)
    db.close()


def test_samosval_grid_distinguishes_same_km_by_volume():
    db, client, _, samosval_type = _setup()
    response = client.post(
        "/settings/tariffs/samosval-grid",
        data={
            "samosval_title": ["15 км / 8 м³", "15 км / 12 м³"],
            "samosval_km": ["15", "15"],
            "samosval_volume": ["8", "12"],
            "samosval_price": ["2800", "3400"],
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    rows = db.query(models.Tariff).order_by(models.Tariff.min_volume).all()
    assert [(r.title, r.min_km, r.max_km, r.min_volume, r.max_volume, r.trip_price) for r in rows] == [
        ("15 км / 8 м³", 15, 15, 8, 8, 2800),
        ("15 км / 12 м³", 15, 15, 12, 12, 3400),
    ]
    vehicle = models.Vehicle(name="Самосвал 1", plate="С001СС78", type_id=samosval_type.id, is_active=True)
    db.add(vehicle)
    db.commit()
    first = app_module._select_tariff(db, models.TripType.SAMOSVAL, vehicle, date(2026, 8, 12), 15, 8)
    second = app_module._select_tariff(db, models.TripType.SAMOSVAL, vehicle, date(2026, 8, 12), 15, 12)
    assert app_module._tariff_amount(first, 15, 8, 1) == 2800
    assert app_module._tariff_amount(second, 15, 12, 1) == 3400
    db.close()


def test_grid_rejects_incomplete_rows():
    db, client, _, _ = _setup()
    response = client.post(
        "/settings/tariffs/samosval-grid",
        data={
            "samosval_title": ["Первый", "Второй"],
            "samosval_km": ["15", "20"],
            "samosval_volume": ["8"],
            "samosval_price": ["2800", "3600"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert db.query(models.Tariff).count() == 0
    db.close()


def test_settings_sections_are_collapsible_and_anchor_driven():
    db, client, _, _ = _setup()
    db.add(models.CargoType(name="Строймусор", unit="м3"))
    db.commit()
    html = client.get("/settings").text

    assert '/static/css/app.css?v=7' in html
    assert '>м³<' in html
    assert '>м3<' not in html
    assert 'class="settings-section-nav"' in html
    for anchor, title in (
        ("vehicle-types", "Типы автомобилей"),
        ("routes", "Объекты и маршруты"),
        ("tariffs", "Тарифы пухтовозов"),
        ("tariffs-samosval", "Тарифы самосвалов"),
        ("vehicles", "Автомобили"),
        ("customers", "Заказчики"),
        ("cargo", "Типы грузов"),
        ("polygons", "Полигоны"),
        ("integrations", "Интеграции"),
    ):
        assert f'href="#{anchor}"' in html
        assert f'id="{anchor}" data-settings-section' in html
        assert title in html

    assert "window.location.hash" in html
    assert "section.open = true" in html
    assert "hashchange" in html
    db.close()


def test_request_form_autoselects_tariff_without_old_vehicle_type_filter():
    db, client, pukhtovoz_type, _ = _setup()
    another_type = models.VehicleType(name="Другой пухтовоз", kind=models.TripType.PUKHTOVOZ)
    vehicle = models.Vehicle(name="Пухтовоз 2", plate="А002АА78", type=another_type, is_active=True)
    tariff = models.Tariff(
        title="Фиксированный рейс", kind=models.TripType.PUKHTOVOZ,
        vehicle_type_id=pukhtovoz_type.id, formula="trip", trip_price=4200, is_active=True,
    )
    db.add_all([another_type, vehicle, tariff])
    db.commit()

    html = client.get("/pukhtovoz/new").text
    assert "Number(t.vehicle_type_id) === vehicleType" not in html
    assert "t.kind === 'пухтовоз' ||" in html
    assert "tariffEl.value = String(list[0].id)" in html
    assert "let tariffManuallySelected = Boolean(tariffEl.value)" in html
    assert "tariffManuallySelected = false" in html
    assert "[kmEl, volEl].forEach(el => el.addEventListener('input', autoRecalc))" in html
    db.close()


def test_samosval_exact_tariff_wins_over_legacy_catch_all():
    db, _, _, samosval_type = _setup()
    vehicle = models.Vehicle(name="Самосвал", plate="С001СС78", type_id=samosval_type.id, is_active=True)
    legacy = models.Tariff(
        title="Старый общий", kind=models.TripType.SAMOSVAL, vehicle_type_id=samosval_type.id,
        formula="trip", trip_price=1000, min_km=0, max_km=None, min_volume=0, max_volume=None, is_active=True,
    )
    exact = models.Tariff(
        title="15 км / 12 м³", kind=models.TripType.SAMOSVAL, vehicle_type_id=samosval_type.id,
        formula="trip", trip_price=3400, min_km=15, max_km=15, min_volume=12, max_volume=12, is_active=True,
    )
    db.add_all([vehicle, legacy, exact])
    db.commit()

    selected = app_module._select_tariff(db, models.TripType.SAMOSVAL, vehicle, date(2026, 8, 12), 15, 12)
    assert selected.id == exact.id
    assert app_module._tariff_amount(selected, 15, 12, 1) == 3400
    db.close()


def test_blank_actual_values_keep_planned_volume_in_polygon_totals():
    db, client, _, samosval_type = _setup()
    driver = models.User(
        full_name="Водитель", login="driver", password_hash=app_module.pwd_hash("pass"),
        role=models.UserRole.DRIVER, is_active=True,
    )
    vehicle = models.Vehicle(name="Самосвал", plate="С002СС78", type_id=samosval_type.id, is_active=True)
    polygon = models.Polygon(name="Северная Самарка", address="")
    db.add_all([driver, vehicle, polygon])
    db.commit()
    for row in (driver, vehicle, polygon):
        db.refresh(row)
    trip = models.TripRequest(
        number="С-1", planned_date=date(2026, 8, 12), driver_id=driver.id, vehicle_id=vehicle.id,
        polygon_id=polygon.id, kind=models.TripType.SAMOSVAL, status=models.RequestStatus.ACCEPTED,
        km=15, volume=12, trips_count=1,
    )
    db.add(trip)
    db.commit()
    db.refresh(trip)
    app_module.app.dependency_overrides[app_module.get_current_user] = lambda: driver

    response = client.post(
        f"/requests/{trip.id}/start", data={"actual_km": "", "actual_volume": ""},
        follow_redirects=False,
    )
    assert response.status_code == 302
    db.refresh(trip)
    assert trip.actual_km == 15
    assert trip.actual_volume == 12
    polygons_html = client.get("/polygons").text
    assert "Северная Самарка" in polygons_html
    assert ">12.0<" in polygons_html or ">12<" in polygons_html
    db.close()


def test_create_request_flows_into_tariff_salary_and_polygon():
    db, client, _, samosval_type = _setup()
    driver = models.User(
        full_name="Иван Водитель", login="driver-flow", password_hash=app_module.pwd_hash("pass"),
        role=models.UserRole.DRIVER, is_active=True,
    )
    vehicle = models.Vehicle(name="Самосвал 12 м³", plate="С012СС78", type_id=samosval_type.id, is_active=True)
    polygon = models.Polygon(name="Северная Самарка", address="Санкт-Петербург")
    legacy = models.Tariff(
        title="Старый общий", kind=models.TripType.SAMOSVAL, vehicle_type_id=samosval_type.id,
        formula="trip", trip_price=1000, min_km=0, max_km=None, min_volume=0, max_volume=None, is_active=True,
    )
    exact = models.Tariff(
        title="15 км / 12 м³", kind=models.TripType.SAMOSVAL, vehicle_type_id=samosval_type.id,
        formula="trip", trip_price=3400, min_km=15, max_km=15, min_volume=12, max_volume=12, is_active=True,
    )
    db.add_all([driver, vehicle, polygon, legacy, exact])
    db.commit()
    for row in (driver, vehicle, polygon, exact):
        db.refresh(row)

    response = client.post("/requests/new", data={
        "number": "С-101", "planned_date": "2026-08-12", "planned_time": "10:00",
        "driver_id": str(driver.id), "vehicle_id": str(vehicle.id), "polygon_id": str(polygon.id),
        "kind": "самосвал", "km": "15", "volume": "12", "trips_count": "1",
        "load_address": "База", "unload_address": "Северная Самарка",
    }, follow_redirects=False)
    assert response.status_code == 303

    trip = db.query(models.TripRequest).filter_by(number="С-101").one()
    assert trip.tariff_id == exact.id
    assert trip.sum_driver == 3400
    assert trip.polygon_id == polygon.id
    assert trip.volume == 12

    trip.status = models.RequestStatus.LOGIST_CONFIRMED
    db.commit()
    salary_html = client.get("/salary").text
    polygons_html = client.get("/polygons").text
    assert "3400" in salary_html
    assert "Северная Самарка" in polygons_html
    assert ">12.0<" in polygons_html or ">12<" in polygons_html
    db.close()

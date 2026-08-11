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

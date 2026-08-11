import os
from datetime import date

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_pukhtovoz_tariffs.db")
os.environ.setdefault("SECRET_KEY", "test-pukhtovoz-tariffs-secret")

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
    vehicle_type = models.VehicleType(name="Пухтовоз", kind=models.TripType.PUKHTOVOZ)
    db.add_all([admin, vehicle_type])
    db.commit()
    db.refresh(admin)
    db.refresh(vehicle_type)
    app_module.app.dependency_overrides[app_module.get_current_user] = lambda: admin
    client = TestClient(app_module.app, headers={"Origin": "http://testserver"})
    return db, client, vehicle_type


def test_pukhtovoz_tariff_form_is_a_simple_repeatable_grid():
    db, client, _ = _setup()

    response = client.get("/settings")

    assert response.status_code == 200
    form = response.text.split('id="pukhtovoz-tariff-form"', 1)[1].split("</form>", 1)[0]
    assert "Расстояние, км" in form
    assert "Объём, м³" in form
    assert "Стоимость рейса, ₽" in form
    assert "Добавить строку тарифа" in form
    for removed in ("Формула", "Коэффициент", "Доплата", "Минимум км", "Максимум км"):
        assert removed not in form
    db.close()


def test_grid_creates_distinct_prices_for_same_distance_and_different_volume():
    db, client, vehicle_type = _setup()

    response = client.post(
        "/settings/tariffs/pukhtovoz-grid",
        data={
            "vehicle_type_id": str(vehicle_type.id),
            "pukhtovoz_km": ["15", "15"],
            "pukhtovoz_volume": ["8", "12"],
            "pukhtovoz_price": ["1200", "1650"],
            "is_active": "on",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    rows = db.query(models.Tariff).order_by(models.Tariff.min_volume).all()
    assert len(rows) == 2
    assert [(row.min_km, row.max_km, row.min_volume, row.max_volume, row.trip_price) for row in rows] == [
        (15, 15, 8, 8, 1200),
        (15, 15, 12, 12, 1650),
    ]
    assert all(row.formula == "trip" and row.kind == models.TripType.PUKHTOVOZ for row in rows)

    vehicle = models.Vehicle(name="Пухтовоз 1", plate="А001АА78", type_id=vehicle_type.id, is_active=True)
    db.add(vehicle)
    db.commit()
    first = app_module._select_tariff(db, models.TripType.PUKHTOVOZ, vehicle, date(2026, 8, 12), 15, 8)
    second = app_module._select_tariff(db, models.TripType.PUKHTOVOZ, vehicle, date(2026, 8, 12), 15, 12)
    assert app_module._tariff_amount(first, 15, 8, 1) == 1200
    assert app_module._tariff_amount(second, 15, 12, 1) == 1650

    edit_page = client.get(f"/settings/tariffs/{first.id}/edit")
    assert edit_page.status_code == 200
    assert 'name="exact_km"' in edit_page.text
    assert 'name="exact_volume"' in edit_page.text
    for removed_name in ("km_price", "volume_price", "fixed_sum", "extra_fee", "coefficient"):
        assert f'name="{removed_name}"' not in edit_page.text

    updated = client.post(
        f"/settings/tariffs/{first.id}/edit",
        data={
            "title": "15 км / 9 м³",
            "kind": "пухтовоз",
            "vehicle_type_id": str(vehicle_type.id),
            "exact_km": "15",
            "exact_volume": "9",
            "trip_price": "1350",
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 302
    db.expire_all()
    saved = db.query(models.Tariff).filter_by(id=first.id).one()
    assert (saved.min_km, saved.max_km, saved.min_volume, saved.max_volume, saved.trip_price) == (15, 15, 9, 9, 1350)
    db.close()


def test_grid_rejects_incomplete_rows():
    db, client, vehicle_type = _setup()

    response = client.post(
        "/settings/tariffs/pukhtovoz-grid",
        data={
            "vehicle_type_id": str(vehicle_type.id),
            "pukhtovoz_km": ["15", "20"],
            "pukhtovoz_volume": ["8"],
            "pukhtovoz_price": ["1200", "1800"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert db.query(models.Tariff).count() == 0
    db.close()

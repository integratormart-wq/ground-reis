import os, io, csv, traceback, sys, math, json
from datetime import datetime, timedelta, date
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, status, Form, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from urllib.parse import urlsplit
from sqlalchemy import func
from jose import jwt
import bcrypt
from openpyxl import Workbook

from backend import models, auth
from backend.auth import create_access_token
from backend.database import SessionLocal, engine
try:
    import backend.bitrix as bitrix
except Exception:
    # заглушка, если модуль bitrix.py ещё не залит — сайт не падает, интеграция спит
    class _BitrixStub:
        @staticmethod
        def sync_trip(*a, **k): return {"skipped": True, "reason": "bitrix_module_missing"}
        @staticmethod
        def delete_trip(*a, **k): return {"skipped": True}
        @staticmethod
        def find_smart_process_ids(*a, **k): return {"_error": "bitrix_module_missing"}
    bitrix = _BitrixStub()
    print("BOOT bitrix module not found — integration disabled until backend/bitrix.py is deployed", flush=True)
from backend.models import UserRole, RequestStatus, TripType, CalcStatus, Polygon, IntegrationSetting, TripArchive

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required")
print("BOOT SECRET_KEY_SET=", True, flush=True)

app = FastAPI(title="GRUND | Рейсы")
BITRIX_LAST_EVENT = {"received": False}
BITRIX_LAST_OUTBOUND = {"attempted": False}

@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/webhook/bitrix24":
        source = request.headers.get("origin") or request.headers.get("referer")
        if not source:
            return JSONResponse({"detail": "Запрос отклонен защитой CSRF"}, status_code=403)
        parsed = urlsplit(source)
        source_host = parsed.netloc.lower()
        request_host = request.headers.get("host", "").lower()
        if parsed.scheme not in {"http", "https"} or source_host != request_host:
            return JSONResponse({"detail": "Запрос отклонен защитой CSRF"}, status_code=403)
    return await call_next(request)

root_dir = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(root_dir, "static"), html=True), name="static")
jinja_env = Environment(loader=FileSystemLoader(os.path.join(root_dir, "templates")), autoescape=select_autoescape(["html", "xml"]))
def render_template(name: str, context: dict) -> HTMLResponse:
    html = jinja_env.get_template(name).render(**context)
    return HTMLResponse(content=html)
UPLOAD_DIR = os.path.join(root_dir, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
print("BOOT creating tables", flush=True)
try:
    models.Base.metadata.create_all(bind=engine)
    print("BOOT tables_created", flush=True)
except Exception as e:
    print("BOOT DB_INIT_ERROR", repr(e), flush=True)
    traceback.print_exc()
    sys.exit(1)

# Авто-миграция: create_all не добавляет колонки в существующие таблицы.
try:
    with engine.begin() as conn:
        from sqlalchemy import inspect, text
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("trip_requests")]
        if "bitrix_element_id" not in cols:
            conn.execute(text("ALTER TABLE trip_requests ADD COLUMN bitrix_element_id INTEGER"))
            print("BOOT migrated: added bitrix_element_id", flush=True)
        else:
            print("BOOT migration: bitrix_element_id already present", flush=True)
        if "bitrix_entity_type_id" not in cols:
            conn.execute(text("ALTER TABLE trip_requests ADD COLUMN bitrix_entity_type_id INTEGER"))
            print("BOOT migrated: added bitrix_entity_type_id", flush=True)
        else:
            print("BOOT migration: bitrix_entity_type_id already present", flush=True)
        duplicate_numbers = conn.execute(text("SELECT number FROM trip_requests GROUP BY number HAVING COUNT(*) > 1 LIMIT 1")).first()
        if duplicate_numbers:
            raise RuntimeError("Найдены дубли номеров заявок; уникальный индекс не создан")
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_trip_requests_number ON trip_requests(number)"))
        duplicate_salary_trip = conn.execute(text("SELECT trip_request_id FROM salary_calc_items GROUP BY trip_request_id HAVING COUNT(*) > 1 LIMIT 1")).first()
        if duplicate_salary_trip:
            raise RuntimeError("Одна заявка включена в несколько расчетов зарплаты; уникальный индекс не создан")
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_salary_calc_items_trip_request ON salary_calc_items(trip_request_id)"))
        if engine.dialect.name == "sqlite":
            conn.execute(text("""
                CREATE TRIGGER IF NOT EXISTS protect_last_active_admin_update
                BEFORE UPDATE OF role, is_active ON users
                WHEN OLD.role = 'ADMIN' AND OLD.is_active = 1
                  AND (NEW.role <> 'ADMIN' OR NEW.is_active <> 1)
                  AND (SELECT COUNT(*) FROM users WHERE role = 'ADMIN' AND is_active = 1 AND id <> OLD.id) = 0
                BEGIN SELECT RAISE(ABORT, 'last active admin'); END
            """))
            conn.execute(text("""
                CREATE TRIGGER IF NOT EXISTS protect_last_active_admin_delete
                BEFORE DELETE ON users
                WHEN OLD.role = 'ADMIN' AND OLD.is_active = 1
                  AND (SELECT COUNT(*) FROM users WHERE role = 'ADMIN' AND is_active = 1 AND id <> OLD.id) = 0
                BEGIN SELECT RAISE(ABORT, 'last active admin'); END
            """))
except Exception as e:
    print("BOOT MIGRATION_ERROR", repr(e), flush=True)
    traceback.print_exc()
    sys.exit(1)
pwd_hash = lambda pw: bcrypt.hashpw(pw[:72].encode(), bcrypt.gensalt()).decode()
pwd_check = lambda pw, h: bcrypt.checkpw(pw[:72].encode(), h.encode())

print("BOOT seed_start", flush=True)
try:
    with SessionLocal() as _db:
        if _db.query(models.User).count() == 0:
            bootstrap_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
            if not bootstrap_password:
                print("BOOT bootstrap_skipped: set BOOTSTRAP_ADMIN_PASSWORD for an empty database", flush=True)
            else:
                if len(bootstrap_password) < 12:
                    raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD must contain at least 12 characters")
                admin = models.User(
                    full_name=os.getenv("BOOTSTRAP_ADMIN_NAME", "Администратор").strip() or "Администратор",
                    login=os.getenv("BOOTSTRAP_ADMIN_LOGIN", "admin").strip() or "admin",
                    password_hash=pwd_hash(bootstrap_password),
                    role=UserRole.ADMIN,
                    is_active=True,
                )
                _db.add(admin)
                _db.commit()
                print("BOOT bootstrap_admin_created", flush=True)
except Exception as e:
    print("BOOT SEED_ERROR", repr(e), flush=True)
    traceback.print_exc()
    sys.exit(1)

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Требуется вход")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Неверный токен")
    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user

def require_role(*allowed_roles):
    def checker(current_user: models.User = Depends(get_current_user)):
        if current_user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Доступ запрещен")
        return current_user
    return checker

def calc_sum(req: models.TripRequest, tariff: Optional[models.Tariff]) -> float:
    return _tariff_amount(tariff, req.actual_km if req.actual_km is not None else (req.km or 0), req.actual_volume if req.actual_volume is not None else (req.volume or 0), req.trips_count if req.trips_count is not None else 1)

def menu_for(role: str):
    if role == UserRole.DRIVER:
        return [
            {"href": "/driver", "label": "Главная"},
            {"href": "/reports", "label": "Отчеты"},
            {"href": "/salary", "label": "Зарплата"},
        ]
    base = [
        {"href": "/dashboard", "label": "Главная"},
        {"href": "/reports", "label": "Отчеты"},
        {"href": "/salary", "label": "Зарплата"},
    ]
    extra = [
        {"href": "/pukhtovoz", "label": "Пухтовозы"},
        {"href": "/samosval", "label": "Самосвалы"},
        {"href": "/polygons", "label": "Полигоны"},
    ]
    base = base[:1] + extra + base[1:]
    if role == UserRole.ADMIN:
        base.append({"href": "/settings", "label": "Настройки"})
        base.append({"href": "/archive", "label": "Архив"})
        base.append({"href": "/users", "label": "Пользователи"})
    return base

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/login")

@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(os.path.join(root_dir, "static", "sw.js"), media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    class _AnonUser:
        full_name = ""
        role = ""
        menu = []
    ctx = {"request": request, "user": _AnonUser(), "app_name": "ГРАУНД | Рейсы", "error": request.query_params.get("error")}
    return render_template("login.html", ctx)

@app.get("/driver", response_class=HTMLResponse)
def driver_panel(request: Request, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != UserRole.DRIVER:
        raise HTTPException(403)
    menu = menu_for(current_user.role)
    today = date.today()
    rows = db.query(models.TripRequest).filter(models.TripRequest.driver_id == current_user.id, models.TripRequest.planned_date == today).all()
    new_reqs = db.query(models.TripRequest).filter(models.TripRequest.driver_id == current_user.id, models.TripRequest.status == RequestStatus.NEW).all()
    work_reqs = db.query(models.TripRequest).filter(models.TripRequest.driver_id == current_user.id, models.TripRequest.status == RequestStatus.IN_WORK).all()
    stats = {"today": len(rows), "pukhtovoz": sum(1 for x in rows if x.kind == TripType.PUKHTOVOZ), "samosval": sum(1 for x in rows if x.kind == TripType.SAMOSVAL), "sum_today": sum(x.sum_driver or 0 for x in rows)}
    return render_template("driver.html", {"request": request, "user": current_user, "menu": menu, "stats": stats, "rows": rows, "new_reqs": new_reqs, "work_reqs": work_reqs, "app_name": "ГРАУНД | Рейсы"})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.login == username).first()
    if not user or not pwd_check(password, user.password_hash):
        return RedirectResponse("/login?error=1", status_code=302)
    token = create_access_token(data={"sub": str(user.id), "role": str(user.role)})
    resp = RedirectResponse("/dashboard", status_code=302)
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"
    resp.set_cookie("access_token", token, httponly=True, secure=is_https, samesite="strict", max_age=60*60*24*30)
    return resp

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role == UserRole.DRIVER:
        return RedirectResponse("/driver", status_code=302)
    menu = menu_for(current_user.role)
    today = date.today()
    stats = {"today": 0, "new": 0, "in_work": 0, "pending_review": 0, "sum_today": 0, "pukhtovoz": 0, "samosval": 0, "sum_pukhtovoz": 0, "sum_samosval": 0}
    if current_user.role == UserRole.DRIVER:
        rows = db.query(models.TripRequest).filter(models.TripRequest.driver_id == current_user.id, models.TripRequest.planned_date == today).all()
        stats["today"] = len(rows)
        stats["sum_today"] = sum(x.sum_driver or 0 for x in rows)
        stats["pukhtovoz"] = sum(1 for x in rows if x.kind == TripType.PUKHTOVOZ)
        stats["samosval"] = sum(1 for x in rows if x.kind == TripType.SAMOSVAL)
        stats["sum_pukhtovoz"] = sum(x.sum_driver or 0 for x in rows if x.kind == TripType.PUKHTOVOZ)
        stats["sum_samosval"] = sum(x.sum_driver or 0 for x in rows if x.kind == TripType.SAMOSVAL)
    else:
        rows_today = db.query(models.TripRequest).filter(models.TripRequest.planned_date == today).all()
        stats["today"] = len(rows_today)
        stats["new"] = db.query(models.TripRequest).filter(models.TripRequest.status == RequestStatus.NEW).count()
        stats["in_work"] = db.query(models.TripRequest).filter(models.TripRequest.status == RequestStatus.IN_WORK).count()
        stats["pending_review"] = db.query(models.TripRequest).filter(models.TripRequest.status == RequestStatus.ON_REVIEW).count()
        stats["sum_today"] = sum(x.sum_driver or 0 for x in rows_today)
        stats["pukhtovoz"] = db.query(models.TripRequest).filter(models.TripRequest.kind == TripType.PUKHTOVOZ).count()
        stats["samosval"] = db.query(models.TripRequest).filter(models.TripRequest.kind == TripType.SAMOSVAL).count()
        stats["sum_pukhtovoz"] = sum(x.sum_driver or 0 for x in rows_today if x.kind == TripType.PUKHTOVOZ)
        stats["sum_samosval"] = sum(x.sum_driver or 0 for x in rows_today if x.kind == TripType.SAMOSVAL)
    return render_template("dashboard.html", {"request": request, "user": current_user, "menu": menu, "stats": stats, "app_name": "ГРАУНД | Рейсы"})

@app.get("/requests", response_class=HTMLResponse)
def requests_list(request: Request, status_f: Optional[str] = None, kind: Optional[str] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    rs = db.query(models.TripRequest)
    if current_user.role == UserRole.DRIVER:
        rs = rs.filter(models.TripRequest.driver_id == current_user.id)
    if status_f:
        try:
            rs = rs.filter(models.TripRequest.status == RequestStatus(status_f))
        except Exception:
            pass
    if kind:
        try:
            rs = rs.filter(models.TripRequest.kind == TripType(kind))
        except Exception:
            pass
    if q:
        rs = rs.filter(models.TripRequest.number.contains(q))
    rs = rs.order_by(models.TripRequest.planned_date.desc()).all()
    drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
    menu = menu_for(current_user.role)
    return render_template("requests.html", {"request": request, "user": current_user, "menu": menu, "reqs": rs, "drivers": drivers, "statuses": RequestStatus, "kind_f": kind or "", "status_f": status_f or "", "q": q or "", "deletable_ids": _deletable_request_ids(db, rs), "app_name": "ГРАУНД | Рейсы"})

@app.get("/pukhtovoz", response_class=HTMLResponse)
def pukhtovoz_list(request: Request, status_f: Optional[str] = None, driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    rs = db.query(models.TripRequest).filter(models.TripRequest.kind == TripType.PUKHTOVOZ)
    if current_user.role == UserRole.DRIVER:
        rs = rs.filter(models.TripRequest.driver_id == current_user.id)
    elif driver_id:
        rs = rs.filter(models.TripRequest.driver_id == int(driver_id))
    if status_f:
        try:
            rs = rs.filter(models.TripRequest.status == RequestStatus(status_f))
        except ValueError:
            pass
    if date_from:
        rs = rs.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to:
        rs = rs.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
    if q:
        rs = rs.filter(models.TripRequest.number.contains(q))
    rs = rs.order_by(models.TripRequest.planned_date.desc()).all()
    drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
    return render_template("trips_kind.html", {"request": request, "user": current_user, "menu": menu, "reqs": rs, "drivers": drivers, "statuses": RequestStatus, "kind_label": "Пухтовозы", "kind": "пухтовоз", "new_url": "/pukhtovoz/new", "status_f": status_f or "", "driver_id": driver_id or "", "date_from": date_from or "", "date_to": date_to or "", "q": q or "", "deletable_ids": _deletable_request_ids(db, rs), "app_name": "ГРАУНД | Рейсы"})

@app.get("/samosval", response_class=HTMLResponse)
def samosval_list(request: Request, status_f: Optional[str] = None, driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    rs = db.query(models.TripRequest).filter(models.TripRequest.kind == TripType.SAMOSVAL)
    if current_user.role == UserRole.DRIVER:
        rs = rs.filter(models.TripRequest.driver_id == current_user.id)
    elif driver_id:
        rs = rs.filter(models.TripRequest.driver_id == int(driver_id))
    if status_f:
        try:
            rs = rs.filter(models.TripRequest.status == RequestStatus(status_f))
        except ValueError:
            pass
    if date_from:
        rs = rs.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to:
        rs = rs.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
    if q:
        rs = rs.filter(models.TripRequest.number.contains(q))
    rs = rs.order_by(models.TripRequest.planned_date.desc()).all()
    drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
    return render_template("trips_kind.html", {"request": request, "user": current_user, "menu": menu, "reqs": rs, "drivers": drivers, "statuses": RequestStatus, "kind_label": "Самосвалы", "kind": "самосвал", "new_url": "/samosval/new", "status_f": status_f or "", "driver_id": driver_id or "", "date_from": date_from or "", "date_to": date_to or "", "q": q or "", "deletable_ids": _deletable_request_ids(db, rs), "app_name": "ГРАУНД | Рейсы"})

@app.get("/requests/new", response_class=HTMLResponse)
def new_request(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    return RedirectResponse("/pukhtovoz/new", status_code=302)

@app.get("/pukhtovoz/new", response_class=HTMLResponse)
def new_pukhtovoz(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    vtypes = db.query(models.VehicleType).all(); vehicles = db.query(models.Vehicle).filter(models.Vehicle.is_active == True).all(); drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all(); customers = db.query(models.Customer).all(); cargo_types = db.query(models.CargoType).all(); polygons = db.query(models.Polygon).all(); tariffs = db.query(models.Tariff).all()
    last = db.query(models.TripRequest).filter(models.TripRequest.kind == TripType.PUKHTOVOZ, models.TripRequest.number.like("П-%")).order_by(models.TripRequest.id.desc()).first()
    next_num = 1
    if last and last.number:
        try:
            next_num = int(str(last.number).split("-", 1)[1]) + 1
        except Exception:
            next_num = 1
    return render_template("request_form.html", {"request": request, "user": current_user, "menu": menu, "vtypes": vtypes, "vehicles": vehicles, "drivers": drivers, "customers": customers, "cargo_types": cargo_types, "polygons": polygons, "tariffs": tariffs, "kind": "пухтовоз", "next_number": next_num, "next_number_hint": f"П-{next_num}", "app_name": "ГРАУНД | Рейсы"})

@app.get("/samosval/new", response_class=HTMLResponse)
def new_samosval(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    vtypes = db.query(models.VehicleType).all(); vehicles = db.query(models.Vehicle).filter(models.Vehicle.is_active == True).all(); drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all(); customers = db.query(models.Customer).all(); cargo_types = db.query(models.CargoType).all(); polygons = db.query(models.Polygon).all(); tariffs = db.query(models.Tariff).all()
    last = db.query(models.TripRequest).filter(models.TripRequest.kind == TripType.SAMOSVAL, models.TripRequest.number.like("С-%")).order_by(models.TripRequest.id.desc()).first()
    next_num = 1
    if last and last.number:
        try:
            next_num = int(str(last.number).split("-", 1)[1]) + 1
        except Exception:
            next_num = 1
    return render_template("request_form.html", {"request": request, "user": current_user, "menu": menu, "vtypes": vtypes, "vehicles": vehicles, "drivers": drivers, "customers": customers, "cargo_types": cargo_types, "polygons": polygons, "tariffs": tariffs, "kind": "самосвал", "next_number": next_num, "next_number_hint": f"С-{next_num}", "app_name": "ГРАУНД | Рейсы"})

@app.post("/requests/new")
def create_request(
    request: Request, number: Optional[str] = Form(None), planned_date: str = Form(...), planned_time: str = Form(""),
    driver_id: Optional[str] = Form(None), vehicle_id: Optional[str] = Form(None), load_address: str = Form(""),
    unload_address: str = Form(""), route_name: str = Form(""), km: str = Form("0"), volume: str = Form("0"),
    trips_count: str = Form("1"), cargo_type_id: Optional[str] = Form(None), customer_id: Optional[str] = Form(None),
    customer_name_manual: Optional[str] = Form(None), polygon_id: Optional[str] = Form(None), kind: str = Form(...),
    comment: str = Form(""), tariff_id: Optional[str] = Form(None),
    current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db),
):
    try:
        kind_value = TripType(kind)
        planned_value = date.fromisoformat(planned_date)
        km_value = _finite_float(km, "Километраж")
        volume_value = _finite_float(volume, "Объем")
        trips_value = int(trips_count or 1)
    except (ValueError, TypeError):
        raise HTTPException(400, "Проверьте дату и числовые поля")
    if km_value < 0 or volume_value < 0 or trips_value <= 0:
        raise HTTPException(400, "Километраж и объем не могут быть отрицательными, число рейсов должно быть больше нуля")
    driver_value = _form_fk(db, models.User, driver_id, "Водитель", required=True)
    driver = db.query(models.User).filter(models.User.id == driver_value).first()
    if driver.role != UserRole.DRIVER or not driver.is_active:
        raise HTTPException(400, "Выберите активного водителя")
    vehicle_value = _form_fk(db, models.Vehicle, vehicle_id, "Автомобиль", required=True)
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.id == vehicle_value).first()
    vehicle_type = db.query(models.VehicleType).filter(models.VehicleType.id == vehicle.type_id).first()
    if not vehicle.is_active or not vehicle_type or vehicle_type.kind != kind_value:
        raise HTTPException(400, "Автомобиль неактивен или не соответствует направлению")
    customer_value = _form_fk(db, models.Customer, customer_id, "Заказчик")
    if not customer_value and customer_name_manual and customer_name_manual.strip():
        clean_customer = customer_name_manual.strip()
        customer = db.query(models.Customer).filter(models.Customer.name == clean_customer).first()
        if not customer:
            customer = models.Customer(name=clean_customer, address="")
            db.add(customer)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                customer = db.query(models.Customer).filter(models.Customer.name == clean_customer).first()
        customer_value = customer.id if customer else None
    cargo_value = _form_fk(db, models.CargoType, cargo_type_id, "Тип груза")
    polygon_value = _form_fk(db, models.Polygon, polygon_id, "Полигон")
    clean_number = (number or "").strip()
    if not clean_number:
        prefix = "П" if kind_value == TripType.PUKHTOVOZ else "С"
        last = db.query(models.TripRequest).filter(models.TripRequest.kind == kind_value, models.TripRequest.number.like(prefix + "-%")).order_by(models.TripRequest.id.desc()).first()
        try:
            next_num = int(str(last.number).split("-", 1)[1]) + 1 if last and last.number else 1
        except (ValueError, IndexError):
            next_num = 1
        clean_number = f"{prefix}-{next_num}"
    if db.query(models.TripRequest).filter(models.TripRequest.number == clean_number).first():
        raise HTTPException(409, "Заявка с таким номером уже существует")
    tariff_value = _form_fk(db, models.Tariff, tariff_id, "Тариф")
    if tariff_value:
        selected = db.query(models.Tariff).filter(models.Tariff.id == tariff_value).first()
        if not _tariff_matches(selected, kind_value, vehicle, planned_value, km_value, volume_value):
            raise HTTPException(400, "Выбранный тариф не подходит к направлению, автомобилю, дате или диапазону")
    tariff = _select_tariff(db, kind_value, vehicle, planned_value, km_value, volume_value, tariff_value)
    if not tariff:
        raise HTTPException(400, "Не найден подходящий активный тариф")
    req = models.TripRequest(
        number=clean_number, planned_date=planned_value, planned_time=planned_time.strip(), driver_id=driver_value,
        vehicle_id=vehicle_value, load_address=load_address.strip(), unload_address=unload_address.strip(),
        route_name=route_name.strip(), km=km_value, volume=volume_value, trips_count=trips_value,
        cargo_type_id=cargo_value, customer_id=customer_value, polygon_id=polygon_value, kind=kind_value,
        status=RequestStatus.ASSIGNED, comment=comment.strip(), tariff_id=tariff.id,
        sum_driver=_tariff_amount(tariff, km_value, volume_value, trips_value),
    )
    req.sum_trip = req.sum_driver
    db.add(req)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Заявка с таким номером уже существует")
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=None, new_status=RequestStatus.ASSIGNED.value))
    _commit_or_conflict(db, "Заявка с таким номером уже существует")
    global BITRIX_LAST_OUTBOUND
    try:
        sync_result = bitrix.sync_trip(req, db)
        BITRIX_LAST_OUTBOUND = {"attempted": True, "request_id": req.id, "kind": req.kind.value, "result": _safe_bitrix_result(sync_result)}
        db.commit()
    except Exception as exc:
        BITRIX_LAST_OUTBOUND = {"attempted": True, "request_id": req.id, "result": {"error": "bitrix_sync_error"}}
        print("BITRIX_SYNC_EXCEPTION", type(exc).__name__, flush=True)
    return RedirectResponse("/pukhtovoz" if req.kind == TripType.PUKHTOVOZ else "/samosval", status_code=303)


def _request_edit_context(request, current_user, db, req):
    return {
        "request": request, "user": current_user, "menu": menu_for(current_user.role),
        "vtypes": db.query(models.VehicleType).all(), "vehicles": db.query(models.Vehicle).filter(models.Vehicle.is_active == True).all(),
        "drivers": db.query(models.User).filter(models.User.role == UserRole.DRIVER).all(),
        "customers": db.query(models.Customer).all(), "cargo_types": db.query(models.CargoType).all(),
        "polygons": db.query(models.Polygon).all(), "tariffs": db.query(models.Tariff).all(),
        "kind": req.kind.value, "editing": req, "next_number": None, "next_number_hint": req.number,
        "app_name": "ГРАУНД | Рейсы",
    }


def _form_fk(db, model, raw_id, label, required=False):
    if raw_id in (None, ""):
        if required:
            raise HTTPException(400, f"Не выбрано поле: {label}")
        return None
    try:
        value = int(raw_id)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Некорректное поле: {label}")
    if not db.query(model).filter(model.id == value).first():
        raise HTTPException(400, f"Не найдено поле: {label}")
    return value


def _finite_float(raw, label, default=0.0, nullable=False):
    if nullable and raw in (None, ""):
        return None
    try:
        value = float(default if raw in (None, "") else raw)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Проверьте поле: {label}")
    if not math.isfinite(value):
        raise HTTPException(400, f"Поле должно быть конечным числом: {label}")
    return value


def _commit_or_conflict(db, detail="Данные конфликтуют с существующей записью"):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, detail)


def _model_snapshot(row, exclude=("password_hash",)):
    result = {}
    for column in row.__table__.columns:
        if column.name in exclude:
            continue
        value = getattr(row, column.name)
        result[column.name] = value.value if hasattr(value, "value") else (value.isoformat() if hasattr(value, "isoformat") else value)
    return json.dumps(result, ensure_ascii=False, default=str)


def _add_audit(db, user_id, section, record_id, old_value, new_value):
    db.add(models.AuditLog(user_id=user_id, action="edit", section=section, record_id=record_id, old_value=old_value, new_value=new_value))


def _export_row(values):
    """Prevent spreadsheet formula execution in user-controlled export cells."""
    whitespace = " " + "".join(chr(code) for code in (9, 10, 11, 12, 13))
    def safe(value):
        if isinstance(value, str) and value.lstrip(whitespace).startswith(("=", "+", "-", "@")):
            return "'" + value
        return value
    return [safe(value) for value in values]


def _salary_sensitive_values(trip):
    return (
        trip.driver_id, trip.planned_date, trip.vehicle_id, trip.kind,
        trip.km, trip.volume, trip.actual_km, trip.actual_volume, trip.trips_count,
        trip.tariff_id, trip.sum_trip, trip.sum_driver, trip.status,
    )


def _safe_bitrix_result(result):
    """Return only typed, non-secret integration diagnostics."""
    if not isinstance(result, dict):
        return {}
    safe = {}
    if isinstance(result.get("ok"), bool):
        safe["ok"] = result["ok"]
    if isinstance(result.get("skipped"), bool):
        safe["skipped"] = result["skipped"]
    for key in ("element_id", "trip_id"):
        if isinstance(result.get(key), int):
            safe[key] = result[key]
    if result.get("action") in {"add", "update", "cancel", "delete"}:
        safe["action"] = result["action"]
    if result.get("reason") in {"no_active_integration", "foreign_process", "disabled"}:
        safe["reason"] = result["reason"]
    if result.get("error"):
        safe["error"] = "bitrix_sync_error"
    return safe


def _safe_bitrix_diagnostic(state):
    if not isinstance(state, dict):
        return {}
    safe = {}
    for key in ("received", "auth_present", "secret_match", "attempted"):
        if isinstance(state.get(key), bool):
            safe[key] = state[key]
    for key in ("item_id", "entity_type_id", "request_id"):
        if isinstance(state.get(key), int):
            safe[key] = state[key]
    if state.get("event") in {"ONCRMDYNAMICITEMADD", "ONCRMDYNAMICITEMUPDATE", "ONCRMDYNAMICITEMDELETE"}:
        safe["event"] = state["event"]
    if state.get("kind") in {member.value for member in TripType}:
        safe["kind"] = state["kind"]
    if "result" in state:
        safe["result"] = _safe_bitrix_result(state["result"])
    return safe


def _sync_from_bitrix_safe(item_id, entity_type_id, db, settings):
    try:
        return bitrix.sync_from_bitrix(item_id, entity_type_id, db, settings=settings)
    except Exception as exc:
        print("BITRIX_INBOUND_EXCEPTION", type(exc).__name__, flush=True)
        return {"error": "bitrix_sync_error"}


def _deletable_request_ids(db, rows):
    final_statuses = {RequestStatus.DRIVER_COMPLETED, RequestStatus.LOGIST_CONFIRMED, RequestStatus.CANCELLED}
    candidates = {row.id for row in rows if row.status not in final_statuses}
    if not candidates:
        return set()
    linked = {item[0] for item in db.query(models.SalaryCalcItem.trip_request_id).filter(models.SalaryCalcItem.trip_request_id.in_(candidates)).all()}
    return candidates - linked


TARIFF_FORMULAS = {"trip", "km", "volume", "fixed"}


def _tariff_matches(tariff, kind, vehicle, planned_date, km, volume):
    return bool(
        tariff and tariff.is_active and tariff.kind == kind
        and (not vehicle or tariff.vehicle_type_id == vehicle.type_id)
        and (tariff.min_km or 0) <= km and (tariff.max_km is None or km <= tariff.max_km)
        and (tariff.min_volume or 0) <= volume and (tariff.max_volume is None or volume <= tariff.max_volume)
        and (tariff.date_from is None or planned_date >= tariff.date_from)
        and (tariff.date_to is None or planned_date <= tariff.date_to)
    )


def _select_tariff(db, kind, vehicle, planned_date, km, volume, preferred_id=None):
    if preferred_id:
        preferred = db.query(models.Tariff).filter(models.Tariff.id == preferred_id).first()
        if preferred and _tariff_matches(preferred, kind, vehicle, planned_date, km, volume):
            return preferred
    candidates = db.query(models.Tariff).filter(
        models.Tariff.kind == kind, models.Tariff.is_active == True
    ).order_by(models.Tariff.id.asc()).all()
    return next((item for item in candidates if _tariff_matches(item, kind, vehicle, planned_date, km, volume)), None)


def _tariff_amount(tariff, km, volume, trips_count):
    if not tariff:
        return 0
    formula = tariff.formula if tariff.formula in TARIFF_FORMULAS else "trip"
    base = {
        "trip": trips_count * (tariff.trip_price or 0),
        "km": km * (tariff.km_price or 0),
        "volume": volume * (tariff.volume_price or 0),
        "fixed": tariff.fixed_sum or 0,
    }[formula]
    return (base + (tariff.extra_fee or 0)) * (tariff.coefficient if tariff.coefficient is not None else 1)


def _validate_tariff_rules(formula, values, min_km, max_km, min_volume, max_volume, date_from, date_to):
    if formula not in TARIFF_FORMULAS:
        raise HTTPException(400, "Некорректная формула тарифа")
    if any(value < 0 for value in values) or min_km < 0 or min_volume < 0:
        raise HTTPException(400, "Значения тарифа не могут быть отрицательными")
    if max_km is not None and min_km > max_km:
        raise HTTPException(400, "Минимальный километраж больше максимального")
    if max_volume is not None and min_volume > max_volume:
        raise HTTPException(400, "Минимальный объем больше максимального")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "Дата начала действия позже даты окончания")


@app.get("/requests/{req_id}/edit", response_class=HTMLResponse)
def edit_request_form(request: Request, req_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req:
        raise HTTPException(404)
    return render_template("request_form.html", _request_edit_context(request, current_user, db, req))


@app.post("/requests/{req_id}/edit")
def edit_request(
    req_id: int, number: str = Form(...), planned_date: str = Form(...), planned_time: str = Form(""),
    driver_id: str = Form(...), vehicle_id: Optional[str] = Form(None), load_address: str = Form(""),
    unload_address: str = Form(""), route_name: str = Form(""), km: str = Form("0"),
    volume: str = Form("0"), trips_count: str = Form("1"), cargo_type_id: Optional[str] = Form(None),
    customer_id: Optional[str] = Form(None), polygon_id: Optional[str] = Form(None), kind: str = Form(...),
    comment: str = Form(""), tariff_id: Optional[str] = Form(None),
    current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db),
):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req:
        raise HTTPException(404)
    old_snapshot = json.dumps({
        "number": req.number, "status": req.status.value, "planned_date": str(req.planned_date), "planned_time": req.planned_time,
        "kind": req.kind.value, "driver_id": req.driver_id, "vehicle_id": req.vehicle_id, "customer_id": req.customer_id,
        "cargo_type_id": req.cargo_type_id, "polygon_id": req.polygon_id, "tariff_id": req.tariff_id,
        "load_address": req.load_address, "unload_address": req.unload_address, "route_name": req.route_name,
        "km": req.km, "volume": req.volume, "trips_count": req.trips_count, "comment": req.comment,
        "sum_trip": req.sum_trip, "sum_driver": req.sum_driver,
    }, ensure_ascii=False)
    driver_value = _form_fk(db, models.User, driver_id, "Водитель", required=True)
    driver = db.query(models.User).filter(models.User.id == driver_value).first()
    if driver.role != UserRole.DRIVER or not driver.is_active:
        raise HTTPException(400, "Выберите активного водителя")
    vehicle_value = _form_fk(db, models.Vehicle, vehicle_id, "Автомобиль", required=True)
    customer_value = _form_fk(db, models.Customer, customer_id, "Заказчик")
    cargo_value = _form_fk(db, models.CargoType, cargo_type_id, "Тип груза")
    polygon_value = _form_fk(db, models.Polygon, polygon_id, "Полигон")
    tariff_value = _form_fk(db, models.Tariff, tariff_id, "Тариф")
    try:
        kind_value = TripType(kind)
        planned_value = date.fromisoformat(planned_date)
        km_value = _finite_float(km, "Километраж")
        volume_value = _finite_float(volume, "Объем")
        trips_value = int(trips_count or 1)
    except (ValueError, TypeError):
        raise HTTPException(400, "Проверьте дату и числовые поля")
    if km_value < 0 or volume_value < 0 or trips_value <= 0:
        raise HTTPException(400, "Километраж и объем не могут быть отрицательными, число рейсов должно быть больше нуля")
    clean_number = number.strip()
    if not clean_number:
        raise HTTPException(400, "Укажите номер заявки")
    if db.query(models.TripRequest).filter(models.TripRequest.number == clean_number, models.TripRequest.id != req.id).first():
        raise HTTPException(409, "Заявка с таким номером уже существует")
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.id == vehicle_value).first()
    vehicle_type = db.query(models.VehicleType).filter(models.VehicleType.id == vehicle.type_id).first()
    if not vehicle.is_active or not vehicle_type or vehicle_type.kind != kind_value:
        raise HTTPException(400, "Автомобиль неактивен или не соответствует направлению")
    final_statuses = {RequestStatus.DRIVER_COMPLETED, RequestStatus.LOGIST_CONFIRMED, RequestStatus.CANCELLED}
    calculation_km = req.actual_km if req.status in final_statuses and req.actual_km is not None else km_value
    calculation_volume = req.actual_volume if req.status in final_statuses and req.actual_volume is not None else volume_value
    if tariff_value:
        selected = db.query(models.Tariff).filter(models.Tariff.id == tariff_value).first()
        if not _tariff_matches(selected, kind_value, vehicle, planned_value, calculation_km, calculation_volume):
            raise HTTPException(400, "Выбранный тариф не подходит к направлению, автомобилю, дате или диапазону")
    tariff = _select_tariff(db, kind_value, vehicle, planned_value, calculation_km, calculation_volume, tariff_value)
    if not tariff:
        raise HTTPException(400, "Не найден подходящий активный тариф")
    salary_items = db.query(models.SalaryCalcItem).filter(models.SalaryCalcItem.trip_request_id == req.id).all()
    new_amount = _tariff_amount(tariff, calculation_km, calculation_volume, trips_value)
    salary_fields_changed = any((
        req.driver_id != driver_value, req.planned_date != planned_value, req.vehicle_id != vehicle_value,
        req.kind != kind_value, (req.km or 0) != km_value, (req.volume or 0) != volume_value,
        (req.trips_count or 1) != trips_value, req.tariff_id != tariff.id, (req.sum_driver or 0) != new_amount,
    ))
    if salary_items and salary_fields_changed:
        raise HTTPException(409, "Нельзя менять расчетные поля заявки, уже включенной в расчет зарплаты")
    req.number = clean_number
    req.planned_date, req.planned_time, req.kind = planned_value, planned_time.strip(), kind_value
    req.driver_id, req.vehicle_id = driver_value, vehicle_value
    req.customer_id, req.cargo_type_id, req.polygon_id = customer_value, cargo_value, polygon_value
    req.load_address, req.unload_address, req.route_name = load_address.strip(), unload_address.strip(), route_name.strip()
    req.km, req.volume, req.trips_count, req.comment = km_value, volume_value, trips_value, comment.strip()
    req.tariff_id = tariff.id
    if not salary_items:
        req.sum_driver = new_amount
        req.sum_trip = new_amount
    new_snapshot = json.dumps({
        "number": req.number, "status": req.status.value, "planned_date": str(req.planned_date), "planned_time": req.planned_time,
        "kind": req.kind.value, "driver_id": req.driver_id, "vehicle_id": req.vehicle_id, "customer_id": req.customer_id,
        "cargo_type_id": req.cargo_type_id, "polygon_id": req.polygon_id, "tariff_id": req.tariff_id,
        "load_address": req.load_address, "unload_address": req.unload_address, "route_name": req.route_name,
        "km": req.km, "volume": req.volume, "trips_count": req.trips_count, "comment": req.comment,
        "sum_trip": req.sum_trip, "sum_driver": req.sum_driver,
    }, ensure_ascii=False)
    db.add(models.AuditLog(user_id=current_user.id, action="edit", section="trip_requests", record_id=req.id, old_value=old_snapshot, new_value=new_snapshot))
    _commit_or_conflict(db, "Не удалось сохранить заявку из-за конфликта данных")
    try:
        bitrix.sync_trip(req, db)
        db.commit()
    except Exception as exc:
        print("BITRIX_SYNC_EXCEPTION", type(exc).__name__, flush=True)
    return RedirectResponse(f"/requests/{req.id}", status_code=302)


@app.get("/requests/{req_id}", response_class=HTMLResponse)
def request_detail(request: Request, req_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or (current_user.role == UserRole.DRIVER and req.driver_id != current_user.id):
        raise HTTPException(404)
    menu = menu_for(current_user.role)
    history = db.query(models.StatusHistory).filter(models.StatusHistory.trip_request_id == req_id).order_by(models.StatusHistory.created_at.desc()).all()
    return render_template("request_detail.html", {"request": request, "user": current_user, "menu": menu, "req": req, "history": history, "can_delete": req.id in _deletable_request_ids(db, [req]), "app_name": "ГРАУНД | Рейсы"})

@app.post("/requests/{req_id}/accept")
def accept_trip(req_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or req.driver_id != current_user.id:
        raise HTTPException(403)
    if req.status != RequestStatus.ASSIGNED:
        raise HTTPException(409, "Принять можно только назначенную заявку")
    req.status = RequestStatus.ACCEPTED
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=RequestStatus.ASSIGNED.value, new_status=RequestStatus.ACCEPTED.value))
    db.commit()
    try:
        bitrix.sync_trip(req, db); db.commit()
    except Exception as e:
        print("BITRIX_SYNC_EXCEPTION", type(e).__name__, flush=True)
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.post("/requests/{req_id}/start")
def start_trip(req_id: int, actual_km: Optional[str] = Form("0"), actual_volume: Optional[str] = Form("0"), current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or req.driver_id != current_user.id:
        raise HTTPException(403)
    if req.status != RequestStatus.ACCEPTED:
        raise HTTPException(409, "Начать можно только принятую заявку")
    km_value = _finite_float(actual_km, "Фактический километраж")
    volume_value = _finite_float(actual_volume, "Фактический объем")
    if km_value < 0 or volume_value < 0:
        raise HTTPException(400, "Фактические значения не могут быть отрицательными")
    req.status = RequestStatus.IN_WORK
    req.started_at = datetime.utcnow()
    req.actual_km, req.actual_volume = km_value, volume_value
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=RequestStatus.ACCEPTED.value, new_status=RequestStatus.IN_WORK.value))
    db.commit()
    try:
        bitrix.sync_trip(req, db); db.commit()
    except Exception as e:
        print("BITRIX_SYNC_EXCEPTION", type(e).__name__, flush=True)
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.post("/requests/{req_id}/complete")
def complete_trip(req_id: int, actual_km: Optional[str] = Form("0"), actual_volume: Optional[str] = Form("0"), comment: str = Form(""), current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or req.driver_id != current_user.id:
        raise HTTPException(403)
    if req.status != RequestStatus.IN_WORK:
        raise HTTPException(409, "Завершить можно только заявку в работе")
    km_value = _finite_float(actual_km, "Фактический километраж")
    volume_value = _finite_float(actual_volume, "Фактический объем")
    if km_value < 0 or volume_value < 0:
        raise HTTPException(400, "Фактические значения не могут быть отрицательными")
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.id == req.vehicle_id).first() if req.vehicle_id else None
    vehicle_type = db.query(models.VehicleType).filter(models.VehicleType.id == vehicle.type_id).first() if vehicle else None
    if not vehicle or not vehicle.is_active or not vehicle_type or vehicle_type.kind != req.kind:
        raise HTTPException(409, "Назначенный автомобиль неактивен или не соответствует направлению")
    tariff = _select_tariff(db, req.kind, vehicle, req.planned_date, km_value, volume_value, req.tariff_id)
    if not tariff:
        raise HTTPException(400, "Не найден подходящий активный тариф")
    old = req.status
    req.status = RequestStatus.DRIVER_COMPLETED
    req.finished_at = datetime.utcnow()
    req.actual_km, req.actual_volume = km_value, volume_value
    clean_comment = comment.strip()
    if clean_comment:
        req.comment = (req.comment or "") + (" | " if req.comment else "") + clean_comment
    req.tariff_id = tariff.id
    req.sum_driver = _tariff_amount(tariff, km_value, volume_value, req.trips_count or 1)
    req.sum_trip = req.sum_driver
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=old.value, new_status=RequestStatus.DRIVER_COMPLETED.value))
    _commit_or_conflict(db)
    try:
        bitrix.sync_trip(req, db); db.commit()
    except Exception as e:
        print("BITRIX_SYNC_EXCEPTION", type(e).__name__, flush=True)
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.post("/requests/{req_id}/confirm")
def confirm_trip(req_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req:
        raise HTTPException(404)
    if req.status != RequestStatus.DRIVER_COMPLETED:
        raise HTTPException(409, "Подтвердить можно только завершенную водителем заявку")
    req.status = RequestStatus.LOGIST_CONFIRMED
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=RequestStatus.DRIVER_COMPLETED.value, new_status=RequestStatus.LOGIST_CONFIRMED.value))
    db.commit()
    try:
        bitrix.sync_trip(req, db); db.commit()
    except Exception as e:
        print("BITRIX_SYNC_EXCEPTION", type(e).__name__, flush=True)
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.get("/salary", response_class=HTMLResponse)
def salary(request: Request, driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(models.TripRequest).filter(models.TripRequest.status == RequestStatus.LOGIST_CONFIRMED)
    if current_user.role == UserRole.DRIVER:
        query = query.filter(models.TripRequest.driver_id == current_user.id)
    else:
        drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
        if driver_id:
            query = query.filter(models.TripRequest.driver_id == int(driver_id))
    if date_from: query = query.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to: query = query.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
    if q: query = query.filter(models.TripRequest.number.contains(q.strip()))
    rows = query.order_by(models.TripRequest.planned_date.desc()).all()
    if current_user.role == UserRole.DRIVER:
        driver = current_user
        trips = rows
        items = [{
            "driver": driver,
            "trips": trips,
            "total": sum((r.sum_driver or 0) for r in trips),
            "pukhtovoz_count": sum(1 for r in trips if r.kind == TripType.PUKHTOVOZ),
            "samosval_count": sum(1 for r in trips if r.kind == TripType.SAMOSVAL),
            "volume": sum((r.actual_volume if r.actual_volume is not None else (r.volume or 0)) for r in trips),
        }]
    else:
        by_driver = {}
        for r in rows:
            by_driver.setdefault(r.driver_id or 0, []).append(r)
        items = []
        for d in drivers:
            trips = by_driver.get(d.id, [])
            items.append({
                "driver": d,
                "trips": trips,
                "total": sum((r.sum_driver or 0) for r in trips),
                "pukhtovoz_count": sum(1 for r in trips if r.kind == TripType.PUKHTOVOZ),
                "samosval_count": sum(1 for r in trips if r.kind == TripType.SAMOSVAL),
                "volume": sum((r.actual_volume if r.actual_volume is not None else (r.volume or 0)) for r in trips),
                })
    menu = menu_for(current_user.role)
    return render_template("salary.html", {"request": request, "user": current_user, "menu": menu, "items": items, "drivers": drivers if current_user.role != UserRole.DRIVER else [current_user], "date_from": date_from, "date_to": date_to, "app_name": "ГРАУНД | Рейсы"})

@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request, status_f: Optional[str] = None, driver_id: Optional[str] = None, polygon_id: Optional[str] = None, customer_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    q_base = db.query(models.TripRequest)
    if current_user.role == UserRole.DRIVER:
        q_base = q_base.filter(models.TripRequest.driver_id == current_user.id)
        driver_id = str(current_user.id)
    if status_f:
        try: q_base = q_base.filter(models.TripRequest.status == RequestStatus(status_f))
        except Exception: pass
    if driver_id: q_base = q_base.filter(models.TripRequest.driver_id == int(driver_id))
    if polygon_id: q_base = q_base.filter(models.TripRequest.polygon_id == int(polygon_id))
    if customer_id: q_base = q_base.filter(models.TripRequest.customer_id == int(customer_id))
    if date_from: q_base = q_base.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to: q_base = q_base.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
    if q:
        like = f"%{q}%"
        q_base = q_base.filter(models.TripRequest.number.ilike(like))
    rows = q_base.order_by(models.TripRequest.planned_date.desc()).all()
    summary = {
        "requests": len(rows),
        "finished": sum(1 for r in rows if r.status == RequestStatus.LOGIST_CONFIRMED),
        "km": sum((r.actual_km if r.actual_km is not None else (r.km or 0)) for r in rows),
        "volume": sum((r.actual_volume if r.actual_volume is not None else (r.volume or 0)) for r in rows),
        "bins": sum((r.waste_bin_count or 0) for r in rows),
        "sum": sum((r.sum_driver or 0) for r in rows),
    }
    return render_template("reports.html", {"request": request, "user": current_user, "menu": menu, "summary": summary, "rows": rows, "statuses": RequestStatus, "drivers": db.query(models.User).filter(models.User.role == UserRole.DRIVER).all(), "polygons": db.query(models.Polygon).all(), "customers": db.query(models.Customer).all(), "status_f": status_f or "", "driver_id": driver_id or "", "polygon_id": polygon_id or "", "customer_id": customer_id or "", "date_from": date_from or "", "date_to": date_to or "", "q": q or "", "app_name": "ГРАУНД | Рейсы"})

@app.get("/export/report.csv")
def export_report(status_f: Optional[str] = None, driver_id: Optional[str] = None, polygon_id: Optional[str] = None, customer_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, search: Optional[str] = Query(None, alias="q"), current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(models.TripRequest)
    if current_user.role == UserRole.DRIVER:
        q = q.filter(models.TripRequest.driver_id == current_user.id)
        driver_id = str(current_user.id)
    if status_f:
        try: q = q.filter(models.TripRequest.status == RequestStatus(status_f))
        except Exception: pass
    if driver_id:
        q = q.filter(models.TripRequest.driver_id == int(driver_id))
    if polygon_id:
        q = q.filter(models.TripRequest.polygon_id == int(polygon_id))
    if customer_id:
        q = q.filter(models.TripRequest.customer_id == int(customer_id))
    if date_from: q = q.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to: q = q.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
    if search: q = q.filter(models.TripRequest.number.ilike(f"%{search}%"))
    rows = q.order_by(models.TripRequest.planned_date.desc()).all()
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(["Номер", "Дата", "Статус", "Водитель", "Полигон", "Компания", "Объем, м3", "Сумма"])
    for r in rows:
        writer.writerow(_export_row([r.number, r.planned_date, r.status.value, r.driver.full_name if r.driver else "", r.polygon.name if r.polygon else "", r.customer.name if r.customer else "", r.actual_volume if r.actual_volume is not None else (r.volume or 0), r.sum_driver or 0]))
    return Response(content="\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=report.csv"})

@app.get("/export/report.xlsx")
def export_report_xlsx(status_f: Optional[str] = None, driver_id: Optional[str] = None, polygon_id: Optional[str] = None, customer_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, search: Optional[str] = Query(None, alias="q"), current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(models.TripRequest)
    if current_user.role == UserRole.DRIVER:
        q = q.filter(models.TripRequest.driver_id == current_user.id)
        driver_id = str(current_user.id)
    if status_f:
        try: q = q.filter(models.TripRequest.status == RequestStatus(status_f))
        except Exception: pass
    if driver_id: q = q.filter(models.TripRequest.driver_id == int(driver_id))
    if polygon_id: q = q.filter(models.TripRequest.polygon_id == int(polygon_id))
    if customer_id: q = q.filter(models.TripRequest.customer_id == int(customer_id))
    if date_from: q = q.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to: q = q.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
    if search: q = q.filter(models.TripRequest.number.ilike(f"%{search}%"))
    rows = q.order_by(models.TripRequest.planned_date.desc()).all()
    wb = Workbook(); ws = wb.active; ws.append(["Номер", "Дата", "Статус", "Водитель", "Полигон", "Компания", "Объем, м3", "Сумма"])
    for r in rows:
        ws.append(_export_row([r.number, r.planned_date, r.status.value, r.driver.full_name if r.driver else "", r.polygon.name if r.polygon else "", r.customer.name if r.customer else "", r.actual_volume if r.actual_volume is not None else (r.volume or 0), r.sum_driver or 0]))
    output = io.BytesIO(); wb.save(output)
    return Response(
        content=output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=report.xlsx"},
    )

def _apply_polygon_filters(query, polygon_id=None, driver_id=None, date_from=None, date_to=None):
    if polygon_id:
        query = query.filter(models.TripRequest.polygon_id == int(polygon_id))
    if driver_id:
        query = query.filter(models.TripRequest.driver_id == int(driver_id))
    if date_from:
        query = query.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to:
        query = query.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
    return query

@app.get("/polygons", response_class=HTMLResponse)
def polygons_list(request: Request, polygon_id: Optional[str] = None, driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    polygons = db.query(models.Polygon).order_by(models.Polygon.name).all()
    visible_polygons = [p for p in polygons if not polygon_id or p.id == int(polygon_id)]
    polygon_query = db.query(models.TripRequest)
    if current_user.role == UserRole.DRIVER:
        polygon_query = polygon_query.filter(models.TripRequest.driver_id == current_user.id)
        driver_id = str(current_user.id)
    filtered = _apply_polygon_filters(polygon_query, polygon_id, driver_id, date_from, date_to).all()
    grouped = {}
    for trip in filtered:
        grouped.setdefault(trip.polygon_id, []).append(trip)
    items = []
    for p in visible_polygons:
        rows = grouped.get(p.id, [])
        items.append({
            "id": p.id,
            "name": p.name,
            "trips": len(rows),
            "volume": sum((r.actual_volume if r.actual_volume is not None else r.volume or 0) for r in rows),
            "sum": sum((r.sum_driver or 0) for r in rows),
        })
    return render_template("polygons.html", {"request": request, "user": current_user, "menu": menu, "items": items, "polygons": polygons, "drivers": db.query(models.User).filter(models.User.role == UserRole.DRIVER).all(), "polygon_id": polygon_id or "", "driver_id": driver_id or "", "date_from": date_from or "", "date_to": date_to or "", "app_name": "ГРАУНД | Рейсы"})

@app.post("/polygons")
def create_polygon(name: str = Form(""), address: str = Form(""), current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    clean_name = name.strip()
    if clean_name and not db.query(models.Polygon).filter(models.Polygon.name == clean_name).first():
        db.add(models.Polygon(name=clean_name, address=address.strip()))
        db.commit()
    return RedirectResponse("/polygons", status_code=302)

@app.get("/export/polygon.csv")
def export_polygon(polygon_id: str, driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    polygon = db.query(models.Polygon).filter(models.Polygon.id == int(polygon_id)).first()
    if not polygon:
        raise HTTPException(404, "Полигон не найден")
    polygon_query = db.query(models.TripRequest)
    if current_user.role == UserRole.DRIVER:
        polygon_query = polygon_query.filter(models.TripRequest.driver_id == current_user.id)
        driver_id = str(current_user.id)
    rows = _apply_polygon_filters(polygon_query, polygon_id, driver_id, date_from, date_to).order_by(models.TripRequest.planned_date, models.TripRequest.id).all()
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(["Полигон", "Номер", "Дата", "Статус", "Водитель", "Автомобиль", "Объем, м3", "Сумма"])
    for r in rows:
        writer.writerow(_export_row([polygon.name, r.number, r.planned_date, r.status.value, r.driver.full_name if r.driver else "", r.vehicle.name if r.vehicle else "", r.actual_volume if r.actual_volume is not None else r.volume or 0, r.sum_driver or 0]))
    return Response(content="\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=polygon-{polygon.id}.csv"})

@app.get("/export/polygons.csv")
def export_polygons(polygon_id: Optional[str] = None, driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    polygon_query = db.query(models.TripRequest)
    if current_user.role == UserRole.DRIVER:
        polygon_query = polygon_query.filter(models.TripRequest.driver_id == current_user.id)
        driver_id = str(current_user.id)
    rows = _apply_polygon_filters(polygon_query, polygon_id, driver_id, date_from, date_to).order_by(models.TripRequest.polygon_id).all()
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(["Полигон", "Заявок", "Объем, м3", "Сумма"])
    groups = {}
    for r in rows:
        key = r.polygon.name if r.polygon else "Без полигона"
        groups.setdefault(key, []).append(r)
    for name, rs in groups.items():
        writer.writerow(_export_row([name, len(rs), sum((r.actual_volume if r.actual_volume is not None else r.volume or 0) for r in rs), sum((r.sum_driver or 0) for r in rs)]))
    return Response(content="\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=polygons.csv"})

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    tariffs = db.query(models.Tariff).all(); vehicles = db.query(models.Vehicle).all(); vtypes = db.query(models.VehicleType).all(); customers = db.query(models.Customer).all(); cargo_types = db.query(models.CargoType).all(); polygons = db.query(models.Polygon).all(); routes = db.query(models.Route).all()
    integrations = {}
    for row in db.query(models.IntegrationSetting).all():
        integrations[row.provider] = row
    return render_template("settings.html", {"request": request, "user": current_user, "menu": menu, "tariffs": tariffs, "vehicles": vehicles, "vtypes": vtypes, "customers": customers, "cargo_types": cargo_types, "polygons": polygons, "routes": routes, "integrations": integrations, "app_name": "ГРАУНД | Рейсы"})

@app.post("/settings/vehicle-types")
def add_vehicle_type(name: str = Form(...), kind: str = Form(...), description: str = Form(""), current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "Укажите тип автомобиля")
    try:
        kind_value = TripType(kind)
    except (ValueError, TypeError):
        raise HTTPException(400, "Некорректное направление")
    if db.query(models.VehicleType).filter(models.VehicleType.name == clean_name).first():
        raise HTTPException(409, "Такой тип автомобиля уже существует")
    db.add(models.VehicleType(name=clean_name, kind=kind_value, description=description.strip()))
    _commit_or_conflict(db)
    return RedirectResponse("/settings#vehicle-types", status_code=302)


@app.post("/settings/routes")
def add_route(name: str = Form(...), load_address: str = Form(""), unload_address: str = Form(""), distance: str = Form("0"), customer_id: Optional[str] = Form(None), comment: str = Form(""), current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "Укажите объект или маршрут")
    if db.query(models.Route).filter(models.Route.name == clean_name).first():
        raise HTTPException(409, "Такой объект или маршрут уже существует")
    distance_value = _finite_float(distance, "Расстояние")
    if distance_value < 0:
        raise HTTPException(400, "Расстояние не может быть отрицательным")
    db.add(models.Route(name=clean_name, load_address=load_address.strip(), unload_address=unload_address.strip(), distance=distance_value, customer_id=_form_fk(db, models.Customer, customer_id, "Заказчик"), comment=comment.strip()))
    _commit_or_conflict(db)
    return RedirectResponse("/settings#routes", status_code=302)


@app.post("/settings/vehicles")
def add_vehicle(name: str = Form(...), plate: str = Form(...), type_id: int = Form(...), capacity: Optional[str] = Form(None), current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    clean_name, clean_plate = name.strip(), plate.strip().upper()
    if db.query(models.Vehicle).filter((models.Vehicle.name == clean_name) | (models.Vehicle.plate == clean_plate)).first():
        return RedirectResponse("/settings?error=vehicle_exists", status_code=302)
    type_value = _form_fk(db, models.VehicleType, type_id, "Тип автомобиля", required=True)
    capacity_value = _finite_float(capacity, "Вместимость", nullable=True)
    if capacity_value is not None and capacity_value < 0:
        raise HTTPException(400, "Вместимость не может быть отрицательной")
    db.add(models.Vehicle(name=clean_name, plate=clean_plate, type_id=type_value, capacity=capacity_value, is_active=True))
    _commit_or_conflict(db)
    return RedirectResponse("/settings#vehicles", status_code=302)

@app.post("/settings/polygons")
def add_settings_polygon(name: str = Form(...), address: str = Form(""), current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    clean_name = name.strip()
    if clean_name and not db.query(models.Polygon).filter(models.Polygon.name == clean_name).first():
        db.add(models.Polygon(name=clean_name, address=address.strip()))
        db.commit()
    return RedirectResponse("/settings#polygons", status_code=302)

@app.post("/settings/customers")
def add_customer(name: str = Form(...), address: str = Form(""), current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    clean_name = name.strip()
    if clean_name and not db.query(models.Customer).filter(models.Customer.name == clean_name).first():
        db.add(models.Customer(name=clean_name, address=address.strip()))
        db.commit()
    return RedirectResponse("/settings#customers", status_code=302)

@app.post("/settings/cargo-types")
def add_cargo_type(name: str = Form(...), unit: str = Form("м3"), current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    clean_name = name.strip()
    if clean_name and not db.query(models.CargoType).filter(models.CargoType.name == clean_name).first():
        db.add(models.CargoType(name=clean_name, unit=unit.strip() or "м3"))
        db.commit()
    return RedirectResponse("/settings#cargo", status_code=302)

@app.post("/settings/tariffs")
def add_tariff(
    title: str = Form(...), kind: str = Form(...), vehicle_type_id: Optional[str] = Form(None), formula: str = Form("trip"),
    trip_price: str = Form("0"), km_price: str = Form("0"), volume_price: str = Form("0"), fixed_sum: str = Form("0"),
    min_km: str = Form("0"), max_km: Optional[str] = Form(None), min_volume: str = Form("0"), max_volume: Optional[str] = Form(None),
    extra_fee: str = Form("0"), coefficient: str = Form("1"), date_from_value: Optional[str] = Form(None, alias="date_from"),
    date_to_value: Optional[str] = Form(None, alias="date_to"), comment: str = Form(""), is_active: Optional[str] = Form(None),
    current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db),
):
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(400, "Укажите название тарифа")
    try:
        kind_enum = TripType(kind)
        prices = [_finite_float(trip_price, "Цена за рейс"), _finite_float(km_price, "Цена за км"), _finite_float(volume_price, "Цена за объем"), _finite_float(fixed_sum, "Фиксированная сумма"), _finite_float(extra_fee, "Доплата"), _finite_float(coefficient, "Коэффициент", default=1)]
        min_km_value, max_km_value = _finite_float(min_km, "Минимум км"), _finite_float(max_km, "Максимум км", nullable=True)
        min_volume_value, max_volume_value = _finite_float(min_volume, "Минимум объема"), _finite_float(max_volume, "Максимум объема", nullable=True)
        from_value = date.fromisoformat(date_from_value) if date_from_value else None
        to_value = date.fromisoformat(date_to_value) if date_to_value else None
    except (ValueError, TypeError):
        raise HTTPException(400, "Проверьте поля тарифа")
    _validate_tariff_rules(formula, prices, min_km_value, max_km_value, min_volume_value, max_volume_value, from_value, to_value)
    vt_id = _form_fk(db, models.VehicleType, vehicle_type_id, "Тип автомобиля", required=True)
    vt = db.query(models.VehicleType).filter(models.VehicleType.id == vt_id).first()
    if vt.kind != kind_enum:
        raise HTTPException(400, "Тип автомобиля не соответствует направлению")
    db.add(models.Tariff(title=clean_title, kind=kind_enum, vehicle_type_id=vt.id, formula=formula,
        trip_price=prices[0], km_price=prices[1], volume_price=prices[2], fixed_sum=prices[3], extra_fee=prices[4], coefficient=prices[5],
        min_km=min_km_value, max_km=max_km_value, min_volume=min_volume_value, max_volume=max_volume_value,
        date_from=from_value, date_to=to_value, comment=comment.strip(), is_active=is_active is not None))
    _commit_or_conflict(db)
    return RedirectResponse("/settings#tariffs", status_code=302)


SETTING_EDIT_MODELS = {
    "vehicle-types": (models.VehicleType, "Тип автомобиля", "vehicle-types"),
    "routes": (models.Route, "Объект / маршрут", "routes"),
    "vehicles": (models.Vehicle, "Автомобиль", "vehicles"),
    "customers": (models.Customer, "Заказчик", "customers"),
    "cargo-types": (models.CargoType, "Тип груза", "cargo"),
    "polygons": (models.Polygon, "Полигон", "polygons"),
    "tariffs": (models.Tariff, "Тариф", "tariffs"),
}


@app.get("/settings/{section}/{record_id}/edit", response_class=HTMLResponse)
def edit_setting_form(request: Request, section: str, record_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    config = SETTING_EDIT_MODELS.get(section)
    if not config:
        raise HTTPException(404)
    model, label, anchor = config
    row = db.query(model).filter(model.id == record_id).first()
    if not row:
        raise HTTPException(404)
    return render_template("settings_edit.html", {
        "request": request, "user": current_user, "menu": menu_for(current_user.role),
        "section": section, "row": row, "label": label, "anchor": anchor,
        "vtypes": db.query(models.VehicleType).all(), "customers": db.query(models.Customer).all(), "app_name": "ГРАУНД | Рейсы",
    })


@app.post("/settings/{section}/{record_id}/edit")
def edit_setting_record(
    section: str, record_id: int, name: Optional[str] = Form(None), title: Optional[str] = Form(None),
    plate: Optional[str] = Form(None), type_id: Optional[str] = Form(None), capacity: Optional[str] = Form(None),
    address: str = Form(""), contact: str = Form(""), phone: str = Form(""), comment: str = Form(""),
    description: str = Form(""), load_address: str = Form(""), unload_address: str = Form(""), distance: str = Form("0"),
    customer_id: Optional[str] = Form(None), unit: str = Form("м3"), kind: Optional[str] = Form(None), vehicle_type_id: Optional[str] = Form(None),
    formula: str = Form("trip"), trip_price: str = Form("0"), km_price: str = Form("0"),
    volume_price: str = Form("0"), fixed_sum: str = Form("0"), min_km: Optional[str] = Form(None),
    max_km: Optional[str] = Form(None), min_volume: Optional[str] = Form(None), max_volume: Optional[str] = Form(None),
    extra_fee: Optional[str] = Form(None), coefficient: Optional[str] = Form(None),
    tariff_date_from: Optional[str] = Form(None, alias="date_from"), tariff_date_to: Optional[str] = Form(None, alias="date_to"),
    is_active: Optional[str] = Form(None),
    current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db),
):
    config = SETTING_EDIT_MODELS.get(section)
    if not config:
        raise HTTPException(404)
    model, _, anchor = config
    row = db.query(model).filter(model.id == record_id).first()
    if not row:
        raise HTTPException(404)
    old_value = _model_snapshot(row)
    if section == "vehicle-types":
        clean_name = (name or "").strip()
        if not clean_name:
            raise HTTPException(400, "Укажите тип автомобиля")
        if db.query(models.VehicleType).filter(models.VehicleType.name == clean_name, models.VehicleType.id != record_id).first():
            raise HTTPException(400, "Такой тип автомобиля уже существует")
        try:
            kind_value = TripType(kind)
        except (ValueError, TypeError):
            raise HTTPException(400, "Некорректное направление")
        if row.kind != kind_value and (db.query(models.Vehicle).filter(models.Vehicle.type_id == row.id).first() or db.query(models.Tariff).filter(models.Tariff.vehicle_type_id == row.id).first()):
            raise HTTPException(409, "Нельзя менять направление используемого типа автомобиля")
        row.name, row.kind, row.description = clean_name, kind_value, description.strip()
    elif section == "routes":
        clean_name = (name or "").strip()
        if not clean_name:
            raise HTTPException(400, "Укажите объект или маршрут")
        if db.query(models.Route).filter(models.Route.name == clean_name, models.Route.id != record_id).first():
            raise HTTPException(400, "Такой объект или маршрут уже существует")
        try:
            distance_value = _finite_float(distance, "Расстояние")
        except ValueError:
            raise HTTPException(400, "Проверьте расстояние")
        if distance_value < 0:
            raise HTTPException(400, "Расстояние не может быть отрицательным")
        row.name, row.distance = clean_name, distance_value
        row.customer_id = _form_fk(db, models.Customer, customer_id, "Заказчик")
        row.load_address, row.unload_address, row.comment = load_address.strip(), unload_address.strip(), comment.strip()
    elif section == "vehicles":
        clean_name, clean_plate = (name or "").strip(), (plate or "").strip().upper()
        if not clean_name or not clean_plate:
            raise HTTPException(400, "Заполните название и госномер")
        duplicate = db.query(models.Vehicle).filter(
            models.Vehicle.id != record_id,
            (models.Vehicle.name == clean_name) | (models.Vehicle.plate == clean_plate),
        ).first()
        if duplicate:
            raise HTTPException(400, "Автомобиль с таким названием или госномером уже существует")
        try:
            capacity_value = _finite_float(capacity, "Вместимость", nullable=True)
        except ValueError:
            raise HTTPException(400, "Проверьте вместимость")
        if capacity_value is not None and capacity_value < 0:
            raise HTTPException(400, "Вместимость не может быть отрицательной")
        new_type_id = _form_fk(db, models.VehicleType, type_id, "Тип автомобиля", required=True)
        vehicle_in_history = (
            db.query(models.TripRequest).filter(models.TripRequest.vehicle_id == row.id).first()
            or db.query(models.TripArchive).filter(models.TripArchive.vehicle_id == row.id).first()
        )
        if row.type_id != new_type_id and vehicle_in_history:
            raise HTTPException(409, "Нельзя менять тип автомобиля, используемого в заявках или архиве")
        row.name, row.plate = clean_name, clean_plate
        row.type_id = new_type_id
        row.capacity, row.is_active = capacity_value, is_active is not None
    elif section == "customers":
        clean_name = (name or "").strip()
        if not clean_name:
            raise HTTPException(400, "Укажите заказчика")
        if db.query(models.Customer).filter(models.Customer.name == clean_name, models.Customer.id != record_id).first():
            raise HTTPException(400, "Такой заказчик уже существует")
        row.name = clean_name
        row.address, row.contact, row.phone, row.comment = address.strip(), contact.strip(), phone.strip(), comment.strip()
    elif section == "cargo-types":
        clean_name = (name or "").strip()
        if not clean_name:
            raise HTTPException(400, "Укажите тип груза")
        if db.query(models.CargoType).filter(models.CargoType.name == clean_name, models.CargoType.id != record_id).first():
            raise HTTPException(400, "Такой тип груза уже существует")
        row.name, row.unit, row.comment = clean_name, unit.strip() or "м3", comment.strip()
    elif section == "polygons":
        clean_name = (name or "").strip()
        if not clean_name:
            raise HTTPException(400, "Укажите полигон")
        if db.query(models.Polygon).filter(models.Polygon.name == clean_name, models.Polygon.id != record_id).first():
            raise HTTPException(400, "Такой полигон уже существует")
        row.name = clean_name
        row.address, row.contact, row.phone, row.comment = address.strip(), contact.strip(), phone.strip(), comment.strip()
    elif section == "tariffs":
        row.title = (title or "").strip()
        if not row.title:
            raise HTTPException(400, "Укажите тариф")
        try:
            row.kind = TripType(kind)
            row.trip_price, row.km_price = _finite_float(trip_price, "Цена за рейс"), _finite_float(km_price, "Цена за км")
            row.volume_price, row.fixed_sum = _finite_float(volume_price, "Цена за объем"), _finite_float(fixed_sum, "Фиксированная сумма")
            if min_km is not None: row.min_km = _finite_float(min_km, "Минимум км")
            if max_km is not None: row.max_km = _finite_float(max_km, "Максимум км", nullable=True)
            if min_volume is not None: row.min_volume = _finite_float(min_volume, "Минимум объема")
            if max_volume is not None: row.max_volume = _finite_float(max_volume, "Максимум объема", nullable=True)
            if extra_fee is not None: row.extra_fee = _finite_float(extra_fee, "Доплата")
            if coefficient is not None: row.coefficient = _finite_float(coefficient, "Коэффициент", default=1)
            if tariff_date_from is not None: row.date_from = date.fromisoformat(tariff_date_from) if tariff_date_from else None
            if tariff_date_to is not None: row.date_to = date.fromisoformat(tariff_date_to) if tariff_date_to else None
        except (ValueError, TypeError):
            raise HTTPException(400, "Проверьте числовые поля тарифа")
        row.vehicle_type_id = _form_fk(db, models.VehicleType, vehicle_type_id, "Тип автомобиля", required=True)
        vehicle_type = db.query(models.VehicleType).filter(models.VehicleType.id == row.vehicle_type_id).first()
        if vehicle_type.kind != row.kind:
            raise HTTPException(400, "Тип автомобиля не соответствует направлению")
        _validate_tariff_rules(
            formula,
            [row.trip_price, row.km_price, row.volume_price, row.fixed_sum, row.extra_fee, row.coefficient],
            row.min_km or 0, row.max_km, row.min_volume or 0, row.max_volume, row.date_from, row.date_to,
        )
        row.formula, row.is_active, row.comment = formula, is_active is not None, comment.strip()
    _add_audit(db, current_user.id, f"settings:{section}", row.id, old_value, _model_snapshot(row))
    _commit_or_conflict(db)
    return RedirectResponse(f"/settings#{anchor}", status_code=302)


@app.post("/settings/{section}/{record_id}/delete")
def delete_setting_record(section: str, record_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    model_map = {"vehicle-types": models.VehicleType, "routes": models.Route, "vehicles": models.Vehicle, "polygons": models.Polygon, "customers": models.Customer, "cargo-types": models.CargoType, "tariffs": models.Tariff}
    model = model_map.get(section)
    if not model:
        raise HTTPException(404)
    row = db.query(model).filter(model.id == record_id).first()
    if row:
        in_use = False
        if section == "vehicle-types":
            in_use = bool(db.query(models.Vehicle).filter(models.Vehicle.type_id == record_id).first() or db.query(models.Tariff).filter(models.Tariff.vehicle_type_id == record_id).first())
        elif section == "vehicles":
            in_use = bool(db.query(models.TripRequest).filter(models.TripRequest.vehicle_id == record_id).first())
        elif section == "polygons":
            in_use = bool(db.query(models.TripRequest).filter(models.TripRequest.polygon_id == record_id).first())
        elif section == "customers":
            in_use = bool(db.query(models.TripRequest).filter(models.TripRequest.customer_id == record_id).first() or db.query(models.Route).filter(models.Route.customer_id == record_id).first())
        elif section == "cargo-types":
            in_use = bool(db.query(models.TripRequest).filter(models.TripRequest.cargo_type_id == record_id).first())
        elif section == "tariffs":
            in_use = bool(db.query(models.TripRequest).filter(models.TripRequest.tariff_id == record_id).first())
        if in_use:
            raise HTTPException(409, "Запись используется и не может быть удалена")
        db.delete(row)
        _commit_or_conflict(db, "Запись используется и не может быть удалена")
    return RedirectResponse("/settings", status_code=302)

@app.post("/settings/integrations")
def save_integrations(provider: str = Form("bitrix24"), webhook_url: str = Form(""), secret: str = Form(""), responsible_id: str = Form(""), current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    row = db.query(models.IntegrationSetting).filter(models.IntegrationSetting.provider == provider).first()
    if not row:
        row = models.IntegrationSetting(provider=provider)
        db.add(row)
    if webhook_url.strip():
        row.webhook_url = webhook_url.strip()
    if secret.strip():
        row.secret = secret.strip()
    row.responsible_id = responsible_id
    row.is_active = bool(row.webhook_url and row.secret)
    db.commit()
    return RedirectResponse("/settings", status_code=302)

@app.get("/settings/bitrix/test", response_class=HTMLResponse)
def bitrix_test(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    row = db.query(models.IntegrationSetting).filter(models.IntegrationSetting.provider == "bitrix24").first()
    info = {"configured": bool(row and row.webhook_url), "processes": [], "error": None}
    if row and row.webhook_url:
        try:
            types = bitrix.find_smart_process_ids(row.webhook_url)
            if "_error" in types:
                info["error"] = "Ошибка подключения к Bitrix24"
            else:
                info["processes"] = [{"id": k, "title": v} for k, v in types.items()]
        except Exception:
            info["error"] = "Ошибка подключения к Bitrix24"
    return render_template("bitrix_test.html", {"request": request, "user": current_user, "menu": menu, "info": info, "app_name": "ГРАУНД | Рейсы"})

@app.post("/webhook/bitrix24")
async def bitrix24_webhook(request: Request, db: Session = Depends(get_db)):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    else:
        form = await request.form()
        payload = {key: value for key, value in form.multi_items()}
    settings = db.query(models.IntegrationSetting).filter(models.IntegrationSetting.provider == "bitrix24").first()
    if not settings or not settings.is_active or not settings.secret:
        raise HTTPException(503, "Активная интеграция и ключ webhook не настроены")
    supplied_secret = request.query_params.get("token") or payload.get("token") or payload.get("auth[application_token]")
    if supplied_secret != settings.secret:
        raise HTTPException(403, "Неверный ключ интеграции")

    global BITRIX_LAST_EVENT
    BITRIX_LAST_EVENT = {
        "received": True,
        "received_at": datetime.now().isoformat(timespec="seconds"),
        "content_type": content_type,
        "keys": sorted(str(key) for key in payload.keys() if "token" not in str(key).lower()),
        "auth_present": True,
        "secret_match": True,
    }
    event, item_id, entity_type_id = bitrix.extract_event_identifiers(payload)
    BITRIX_LAST_EVENT.update({"event": event, "item_id": item_id, "entity_type_id": entity_type_id})
    if event not in {"ONCRMDYNAMICITEMADD", "ONCRMDYNAMICITEMUPDATE", "ONCRMDYNAMICITEMDELETE"}:
        BITRIX_LAST_EVENT["result"] = "unsupported_event"
        return JSONResponse({"ok": True, "skipped": "unsupported_event", "event": event})
    if not item_id or not entity_type_id:
        BITRIX_LAST_EVENT["result"] = "missing_item_or_entity"
        return JSONResponse({"ok": False, "error": "missing_item_or_entity"}, status_code=400)

    trip = db.query(models.TripRequest).filter(
        models.TripRequest.bitrix_element_id == item_id,
        models.TripRequest.bitrix_entity_type_id == entity_type_id,
    ).first()
    if event == "ONCRMDYNAMICITEMDELETE":
        if trip:
            if trip.status in {RequestStatus.DRIVER_COMPLETED, RequestStatus.LOGIST_CONFIRMED, RequestStatus.CANCELLED} or db.query(models.SalaryCalcItem).filter_by(trip_request_id=trip.id).first():
                raise HTTPException(409, "Финальную или включенную в зарплату заявку нельзя отменить через webhook")
            old_status = trip.status
            trip.status = RequestStatus.CANCELLED
            db.add(models.StatusHistory(trip_request_id=trip.id, old_status=old_status.value, new_status=RequestStatus.CANCELLED.value))
            db.commit()
        BITRIX_LAST_EVENT["result"] = "cancel"
        return JSONResponse({"ok": True, "action": "cancel", "trip_id": trip.id if trip else None})

    old_status = trip.status if trip else None
    old_salary_values = None
    salary_locked = False
    if trip:
        old_salary_values = _salary_sensitive_values(trip)
        salary_locked = bool(db.query(models.SalaryCalcItem).filter_by(trip_request_id=trip.id).first())
    result = _sync_from_bitrix_safe(item_id, entity_type_id, db, settings)
    if result.get("error"):
        db.rollback()
        safe_result = _safe_bitrix_result(result)
        BITRIX_LAST_EVENT["result"] = safe_result
        return JSONResponse(safe_result, status_code=400)
    if result.get("action") == "update" and old_status is None:
        # sync_from_bitrix may match an existing row by number+kind when a new Bitrix ID arrives.
        # Roll back its first mutation, snapshot the actual row, then repeat under lifecycle/salary guards.
        db.rollback()
        trip = db.query(models.TripRequest).filter(models.TripRequest.id == result.get("trip_id")).first()
        if not trip:
            raise HTTPException(502, "Bitrix не вернул локальную заявку")
        old_status = trip.status
        old_salary_values = _salary_sensitive_values(trip)
        salary_locked = bool(db.query(models.SalaryCalcItem).filter_by(trip_request_id=trip.id).first())
        result = _sync_from_bitrix_safe(item_id, entity_type_id, db, settings)
        if result.get("error"):
            db.rollback()
            safe_result = _safe_bitrix_result(result)
            BITRIX_LAST_EVENT["result"] = safe_result
            return JSONResponse(safe_result, status_code=400)
    trip = db.query(models.TripRequest).filter(models.TripRequest.id == result.get("trip_id")).first()
    if not trip:
        db.rollback()
        raise HTTPException(502, "Bitrix не вернул локальную заявку")
    if old_status is None and trip.status != RequestStatus.NEW:
        trip.status = RequestStatus.NEW
    if old_status is not None and trip.status != old_status:
        allowed_transitions = {
            RequestStatus.NEW: {RequestStatus.ASSIGNED, RequestStatus.CANCELLED},
            RequestStatus.ASSIGNED: {RequestStatus.ACCEPTED, RequestStatus.CANCELLED},
            RequestStatus.ACCEPTED: {RequestStatus.IN_WORK, RequestStatus.CANCELLED},
            RequestStatus.IN_WORK: {RequestStatus.DRIVER_COMPLETED, RequestStatus.CANCELLED},
            RequestStatus.DRIVER_COMPLETED: {RequestStatus.ON_REVIEW, RequestStatus.LOGIST_CONFIRMED, RequestStatus.NEEDS_CORRECTION},
            RequestStatus.ON_REVIEW: {RequestStatus.LOGIST_CONFIRMED, RequestStatus.NEEDS_CORRECTION},
            RequestStatus.NEEDS_CORRECTION: {RequestStatus.IN_WORK, RequestStatus.CANCELLED},
        }
        if trip.status not in allowed_transitions.get(old_status, set()):
            db.rollback()
            raise HTTPException(409, "Недопустимый переход статуса через webhook")
    if trip.status != RequestStatus.NEW:
        driver = db.query(models.User).filter_by(id=trip.driver_id).first() if trip.driver_id else None
        vehicle = db.query(models.Vehicle).filter_by(id=trip.vehicle_id).first() if trip.vehicle_id else None
        if not driver or driver.role != UserRole.DRIVER or not driver.is_active:
            db.rollback(); raise HTTPException(400, "Webhook назначил неактивного или некорректного водителя")
        if not vehicle or not vehicle.is_active or not vehicle.type or vehicle.type.kind != trip.kind:
            db.rollback(); raise HTTPException(400, "Webhook назначил неактивный или несовместимый автомобиль")
        km_value = trip.actual_km if trip.actual_km is not None else (trip.km or 0)
        volume_value = trip.actual_volume if trip.actual_volume is not None else (trip.volume or 0)
        tariff = _select_tariff(db, trip.kind, vehicle, trip.planned_date, km_value, volume_value, trip.tariff_id)
        if not tariff:
            db.rollback(); raise HTTPException(400, "Для данных webhook не найден совместимый тариф")
        trip.tariff_id = tariff.id
        amount = _tariff_amount(tariff, km_value, volume_value, trip.trips_count if trip.trips_count is not None else 1)
        trip.sum_trip = trip.sum_driver = amount
    if salary_locked:
        new_salary_values = _salary_sensitive_values(trip)
        if new_salary_values != old_salary_values:
            db.rollback(); raise HTTPException(409, "Заявка включена в расчет зарплаты")
    if old_status is None:
        db.add(models.StatusHistory(trip_request_id=trip.id, old_status=None, new_status=trip.status.value))
    elif trip.status != old_status:
        db.add(models.StatusHistory(trip_request_id=trip.id, old_status=old_status.value, new_status=trip.status.value))
    db.commit()
    safe_result = _safe_bitrix_result(result)
    BITRIX_LAST_EVENT["result"] = safe_result
    return JSONResponse(safe_result)

@app.get("/settings/bitrix/status")
def bitrix24_status(current_user: models.User = Depends(require_role(UserRole.ADMIN))):
    # Только безопасные метаданные без webhook URL и токенов.
    return JSONResponse({"inbound": _safe_bitrix_diagnostic(BITRIX_LAST_EVENT), "outbound": _safe_bitrix_diagnostic(BITRIX_LAST_OUTBOUND)})

@app.post("/requests/{req_id}/delete")
def delete_request(req_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req:
        raise HTTPException(404)
    if req.status in {RequestStatus.DRIVER_COMPLETED, RequestStatus.LOGIST_CONFIRMED, RequestStatus.CANCELLED}:
        raise HTTPException(409, "Завершенную, подтвержденную или отмененную заявку удалять нельзя")
    if db.query(models.SalaryCalcItem).filter(models.SalaryCalcItem.trip_request_id == req.id).first():
        raise HTTPException(409, "Заявка включена в расчет зарплаты и не может быть удалена")
    db.query(models.StatusHistory).filter(models.StatusHistory.trip_request_id == req.id).delete(
        synchronize_session=False
    )
    db.delete(req)
    _commit_or_conflict(db, "Заявка связана с другими данными и не может быть удалена")
    try:
        bitrix.delete_trip(req, db)
        db.commit()
    except Exception as exc:
        print("BITRIX_DELETE_EXCEPTION", type(exc).__name__, flush=True)
    return RedirectResponse("/requests", status_code=302)

@app.get("/archive", response_class=HTMLResponse)
def archive_list(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    rows = db.query(models.TripArchive).order_by(models.TripArchive.archived_at.desc()).all()
    return render_template("archive.html", {"request": request, "user": current_user, "menu": menu, "rows": rows, "app_name": "ГРАУНД | Рейсы"})

@app.post("/archive/{archive_id}/restore")
def restore_archive(archive_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    item = db.query(models.TripArchive).filter(models.TripArchive.id == archive_id).first()
    if not item:
        raise HTTPException(404)
    req = models.TripRequest(
        number=item.number, planned_date=item.planned_date, planned_time=item.planned_time,
        driver_id=item.driver_id, vehicle_id=item.vehicle_id, load_address=item.load_address, unload_address=item.unload_address,
        route_name=item.route_name, km=item.km, volume=item.volume, trips_count=item.trips_count,
        cargo_type_id=item.cargo_type_id, customer_id=item.customer_id, kind=item.kind, status=item.status,
        started_at=item.started_at, finished_at=item.finished_at, actual_km=item.actual_km, actual_volume=item.actual_volume,
        sum_trip=item.sum_trip, sum_driver=item.sum_driver, tariff_id=item.tariff_id, comment=item.comment,
        logist_comment=item.logist_comment, polygon_id=item.polygon_id, waste_bin_count=item.waste_bin_count
    )
    db.add(req)
    db.delete(item)
    db.commit()
    return RedirectResponse("/archive", status_code=302)

@app.get("/users", response_class=HTMLResponse)
def users_list(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    users = db.query(models.User).all()
    return render_template("users.html", {"request": request, "user": current_user, "menu": menu, "users": users, "app_name": "ГРАУНД | Рейсы"})

@app.get("/users/new", response_class=HTMLResponse)
def new_user_form(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    return render_template("users.html", {"request": request, "user": current_user, "menu": menu, "users": db.query(models.User).all(), "editing": None, "app_name": "ГРАУНД | Рейсы"})

@app.post("/users/new")
def create_user(full_name: str = Form(...), login: str = Form(...), password: str = Form(...), role: str = Form(...), current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    clean_name, clean_login = full_name.strip(), login.strip()
    if not clean_name or not clean_login:
        raise HTTPException(400, "Заполните имя и логин")
    if len(password.strip()) < 6:
        raise HTTPException(400, "Пароль должен содержать не менее 6 символов")
    if db.query(models.User).filter(models.User.login == clean_login).first():
        raise HTTPException(409, "Такой логин уже используется")
    try:
        role_value = UserRole(role)
    except (ValueError, TypeError):
        raise HTTPException(400, "Некорректная роль")
    u = models.User(full_name=clean_name, login=clean_login, password_hash=pwd_hash(password), role=role_value, is_active=True)
    db.add(u)
    _commit_or_conflict(db)
    return RedirectResponse("/users", status_code=302)

@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_form(request: Request, user_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    editing = db.query(models.User).filter(models.User.id == user_id).first()
    if not editing:
        raise HTTPException(404)
    return render_template("users.html", {
        "request": request, "user": current_user, "menu": menu_for(current_user.role),
        "users": db.query(models.User).all(), "editing": editing, "app_name": "ГРАУНД | Рейсы",
    })


@app.post("/users/{user_id}/edit")
def edit_user(
    user_id: int, full_name: str = Form(...), login: str = Form(...), password: str = Form(""),
    role: str = Form(...), phone: str = Form(""), is_active: Optional[str] = Form(None),
    current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db),
):
    editing = db.query(models.User).filter(models.User.id == user_id).first()
    if not editing:
        raise HTTPException(404)
    old_value = _model_snapshot(editing)
    clean_name, clean_login = full_name.strip(), login.strip()
    if not clean_name or not clean_login:
        raise HTTPException(400, "Заполните имя и логин")
    duplicate = db.query(models.User).filter(models.User.login == clean_login, models.User.id != user_id).first()
    if duplicate:
        raise HTTPException(400, "Такой логин уже используется")
    try:
        role_value = UserRole(role)
    except (ValueError, TypeError):
        raise HTTPException(400, "Некорректная роль")
    active_value = is_active is not None
    if editing.id == current_user.id and (role_value != UserRole.ADMIN or not active_value):
        raise HTTPException(400, "Нельзя снять у себя роль администратора или деактивировать себя")
    if editing.role == UserRole.ADMIN and editing.is_active and (role_value != UserRole.ADMIN or not active_value):
        active_admins = db.query(models.User).filter(models.User.role == UserRole.ADMIN, models.User.is_active == True).with_for_update().all()
        if len(active_admins) <= 1:
            raise HTTPException(400, "Нельзя отключить последнего активного администратора")
    editing.full_name, editing.login = clean_name, clean_login
    editing.role, editing.phone, editing.is_active = role_value, phone.strip(), active_value
    if password and password.strip():
        clean_password = password.strip()
        if len(clean_password) < 6:
            raise HTTPException(400, "Новый пароль должен содержать не менее 6 символов")
        editing.password_hash = pwd_hash(clean_password)
    _add_audit(db, current_user.id, "users", editing.id, old_value, _model_snapshot(editing))
    _commit_or_conflict(db)
    return RedirectResponse("/users", status_code=302)


@app.post("/users/{user_id}/delete")
def delete_user(user_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404)
    if user.id == current_user.id:
        raise HTTPException(400, "Нельзя удалить собственную учетную запись")
    if user.role == UserRole.ADMIN and user.is_active:
        active_admins = db.query(models.User).filter(models.User.role == UserRole.ADMIN, models.User.is_active == True).with_for_update().all()
        if len(active_admins) <= 1:
            raise HTTPException(400, "Нельзя удалить последнего активного администратора")
    linked = (
        db.query(models.TripRequest).filter(models.TripRequest.driver_id == user.id).first()
        or db.query(models.TripArchive).filter(models.TripArchive.driver_id == user.id).first()
        or db.query(models.SalaryCalc).filter(models.SalaryCalc.driver_id == user.id).first()
    )
    if linked:
        raise HTTPException(409, "Пользователь связан с заявками или расчетами и не может быть удален")
    db.query(models.StatusHistory).filter(models.StatusHistory.user_id == user.id).update({"user_id": None}, synchronize_session=False)
    db.query(models.AuditLog).filter(models.AuditLog.user_id == user.id).update({"user_id": None}, synchronize_session=False)
    db.delete(user)
    _commit_or_conflict(db, "Пользователь связан с данными и не может быть удален")
    return RedirectResponse("/users", status_code=303)

@app.post("/logout")
def logout(current_user: models.User = Depends(get_current_user)):
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp

@app.get("/export/requests.csv")
def export_csv(status_f: Optional[str] = None, kind: Optional[str] = None, driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(models.TripRequest)
    if current_user.role == UserRole.DRIVER:
        q = q.filter(models.TripRequest.driver_id == current_user.id)
        driver_id = str(current_user.id)
    if status_f:
        try:
            q = q.filter(models.TripRequest.status == RequestStatus(status_f))
        except ValueError:
            pass
    if kind:
        try:
            q = q.filter(models.TripRequest.kind == TripType(kind))
        except ValueError:
            pass
    if driver_id:
        q = q.filter(models.TripRequest.driver_id == int(driver_id))
    if date_from:
        q = q.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to:
        q = q.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
    rows = q.order_by(models.TripRequest.planned_date.desc()).all()
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(["Номер", "Дата", "Статус", "Водитель", "Автомобиль", "Сумма водителю"])
    for r in rows:
        writer.writerow(_export_row([r.number, r.planned_date, r.status.value, r.driver.full_name if r.driver else "", r.vehicle.name if r.vehicle else "", r.sum_driver or 0]))
    return Response(content="\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=requests.csv"})

@app.get("/export/salary.xlsx")
def export_xlsx(driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(models.TripRequest).filter(models.TripRequest.status == RequestStatus.LOGIST_CONFIRMED)
    if current_user.role == UserRole.DRIVER:
        query = query.filter(models.TripRequest.driver_id == current_user.id)
    elif driver_id:
        query = query.filter(models.TripRequest.driver_id == int(driver_id))
    if date_from: query = query.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to: query = query.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
    if q: query = query.filter(models.TripRequest.number.contains(q.strip()))
    trips = query.order_by(models.TripRequest.driver_id, models.TripRequest.planned_date).all()
    grouped = {}
    for trip in trips:
        grouped.setdefault(trip.driver_id, []).append(trip)
    wb = Workbook(); ws = wb.active; ws.append(["Водитель", "Кол-во заявок", "Километраж", "Объем", "Сумма"])
    for driver_trips in grouped.values():
        d = driver_trips[0].driver
        ws.append(_export_row([d.full_name if d else "", len(driver_trips), sum(t.actual_km if t.actual_km is not None else (t.km or 0) for t in driver_trips), sum(t.actual_volume if t.actual_volume is not None else (t.volume or 0) for t in driver_trips), sum(t.sum_driver or 0 for t in driver_trips)]))
    output = io.BytesIO(); wb.save(output)
    return Response(
        content=output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=salary.xlsx"},
    )

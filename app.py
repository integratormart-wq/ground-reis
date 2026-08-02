import os, io, csv, traceback, sys
from datetime import datetime, timedelta, date
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, status, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session
from sqlalchemy import func
from jose import jwt
import bcrypt
from openpyxl import Workbook

from backend import models, auth
from backend.auth import create_access_token
from backend.database import SessionLocal, engine
from backend.models import UserRole, RequestStatus, TripType, CalcStatus, Polygon, IntegrationSetting, TripArchive

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY", "ground_secret_key_2026")
print("BOOT SECRET_KEY_SET=", bool(SECRET_KEY), flush=True)

app = FastAPI(title="GRUND | Рейсы")
root_dir = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(root_dir, "static"), html=True), name="static")
jinja_env = Environment(loader=FileSystemLoader(os.path.join(root_dir, "templates")), autoescape=False)
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
pwd_hash = lambda pw: bcrypt.hashpw(pw[:72].encode(), bcrypt.gensalt()).decode()
pwd_check = lambda pw, h: bcrypt.checkpw(pw[:72].encode(), h.encode())

print("BOOT seed_start", flush=True)
try:
    with SessionLocal() as _db:
        if _db.query(models.User).count() == 0:
            admin = models.User(full_name="Администратор", login="admin", password_hash=pwd_hash("admin123"), role=UserRole.ADMIN, is_active=True)
            logist = models.User(full_name="Логист Иван", login="logist", password_hash=pwd_hash("logist123"), role=UserRole.LOGIST, is_active=True)
            driver1 = models.User(full_name="Петров Петр", login="driver1", password_hash=pwd_hash("driver123"), role=UserRole.DRIVER, is_active=True)
            driver2 = models.User(full_name="Сидоров Алексей", login="driver2", password_hash=pwd_hash("driver123"), role=UserRole.DRIVER, is_active=True)
            _db.add_all([admin, logist, driver1, driver2]); _db.flush()
            vt1 = models.VehicleType(name="КАМАЗ пухтовоз", kind=TripType.PUKHTOVOZ)
            vt2 = models.VehicleType(name="Урал самосвал", kind=TripType.SAMOSVAL)
            _db.add_all([vt1, vt2]); _db.flush()
            v1 = models.Vehicle(name="КАМАЗ-65115", plate="А 123 БС 78", type_id=vt1.id, capacity=12)
            v2 = models.Vehicle(name="Урал-6560", plate="В 456 КТ 99", type_id=vt2.id, capacity=20)
            _db.add_all([v1, v2]); _db.flush()
            cust = models.Customer(name="ООО СтройГарант", address="СПб, пр. Просвещения, 12")
            _db.add(cust); _db.flush()
            cargo = models.CargoType(name="Строймусор", unit="м3")
            _db.add(cargo); _db.flush()
            t1 = models.Tariff(title="Пухтовоз до 15км", vehicle_type_id=vt1.id, kind=TripType.PUKHTOVOZ, min_km=0, max_km=15, trip_price=3500, formula="trip")
            t2 = models.Tariff(title="Самосвал до 15км", vehicle_type_id=vt2.id, kind=TripType.SAMOSVAL, min_km=0, max_km=15, trip_price=2800, formula="trip")
            t3 = models.Tariff(title="Пухтовоз 15-30км", vehicle_type_id=vt1.id, kind=TripType.PUKHTOVOZ, min_km=15, max_km=30, trip_price=4200, formula="trip")
            _db.add_all([t1, t2, t3])
            for i in range(1,4):
                req = models.TripRequest(
                    number=f"П-2026-{i:03d}", planned_date=date.today() - timedelta(days=i),
                    planned_time="08:00" if i%2 else "14:00",
                    driver_id=driver1.id if i%2 else driver2.id,
                    vehicle_id=v1.id if i%2 else v2.id,
                    load_address="СПб, ул. Строителей, 1", unload_address="Полигон Ленинградская обл.",
                    route_name="Маршрут А", km=12+i, volume=8+i, trips_count=1,
                    cargo_type_id=cargo.id, customer_id=cust.id,
                    kind=TripType.PUKHTOVOZ if i%2 else TripType.SAMOSVAL,
                    status=RequestStatus.LOGIST_CONFIRMED, actual_km=12+i, actual_volume=8+i,
                    sum_trip=3500+i*200, sum_driver=3500+i*200, tariff_id=t1.id if i%2 else t2.id,
                    logist_comment="Без замечаний"
                )
                _db.add(req); _db.flush()
                _db.add(models.StatusHistory(trip_request_id=req.id, user_id=logist.id, old_status=RequestStatus.NEW.value, new_status=RequestStatus.LOGIST_CONFIRMED.value))
            _db.commit()
            print("BOOT seed_ok", flush=True)
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
    user = _db.query(models.User).filter(models.User.id == int(user_id)).first()
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
    if not tariff:
        return 0.0
    trips = req.trips_count or 1
    km = req.actual_km or req.km or 0
    vol = req.actual_volume or req.volume or 0
    if tariff.formula == "km":
        return km * (tariff.km_price or 0)
    if tariff.formula == "volume":
        return vol * (tariff.volume_price or 0)
    if tariff.formula == "fixed":
        return tariff.fixed_sum or 0
    return trips * (tariff.trip_price or 0)

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
    if role in (UserRole.ADMIN, UserRole.LOGIST):
        base.append({"href": "/settings", "label": "Настройки"})
        base.append({"href": "/archive", "label": "Архив"})
    if role == UserRole.ADMIN:
        base.append({"href": "/users", "label": "Пользователи"})
    return base

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/login")

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
    resp.set_cookie("access_token", token, httponly=True, max_age=60*60*24*30)
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
    return render_template("requests.html", {"request": request, "user": current_user, "menu": menu, "reqs": rs, "drivers": drivers, "statuses": RequestStatus, "kind_f": kind or "", "status_f": status_f or "", "q": q or "", "app_name": "ГРАУНД | Рейсы"})

@app.get("/pukhtovoz", response_class=HTMLResponse)
def pukhtovoz_list(request: Request, status_f: Optional[str] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    rs = db.query(models.TripRequest).filter(models.TripRequest.kind == TripType.PUKHTOVOZ)
    if current_user.role == UserRole.DRIVER:
        rs = rs.filter(models.TripRequest.driver_id == current_user.id)
    if status_f:
        rs = rs.filter(models.TripRequest.status == RequestStatus(status_f))
    if q:
        rs = rs.filter(models.TripRequest.number.contains(q))
    rs = rs.order_by(models.TripRequest.planned_date.desc()).all()
    drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
    return render_template("trips_kind.html", {"request": request, "user": current_user, "menu": menu, "reqs": rs, "drivers": drivers, "statuses": RequestStatus, "kind_label": "Пухтовозы", "kind": "пухтовоз", "new_url": "/pukhtovoz/new", "app_name": "ГРАУНД | Рейсы"})

@app.get("/samosval", response_class=HTMLResponse)
def samosval_list(request: Request, status_f: Optional[str] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    rs = db.query(models.TripRequest).filter(models.TripRequest.kind == TripType.SAMOSVAL)
    if current_user.role == UserRole.DRIVER:
        rs = rs.filter(models.TripRequest.driver_id == current_user.id)
    if status_f:
        rs = rs.filter(models.TripRequest.status == RequestStatus(status_f))
    if q:
        rs = rs.filter(models.TripRequest.number.contains(q))
    rs = rs.order_by(models.TripRequest.planned_date.desc()).all()
    drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
    return render_template("trips_kind.html", {"request": request, "user": current_user, "menu": menu, "reqs": rs, "drivers": drivers, "statuses": RequestStatus, "kind_label": "Самосвалы", "kind": "самосвал", "new_url": "/samosval/new", "app_name": "ГРАУНД | Рейсы"})

@app.get("/requests/new", response_class=HTMLResponse)
def new_request(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    return RedirectResponse("/pukhtovoz/new", status_code=302)

@app.get("/pukhtovoz/new", response_class=HTMLResponse)
def new_pukhtovoz(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    vtypes = db.query(models.VehicleType).all(); vehicles = db.query(models.Vehicle).all(); drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all(); customers = db.query(models.Customer).all(); cargo_types = db.query(models.CargoType).all(); polygons = db.query(models.Polygon).all(); tariffs = db.query(models.Tariff).all()
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
    vtypes = db.query(models.VehicleType).all(); vehicles = db.query(models.Vehicle).all(); drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all(); customers = db.query(models.Customer).all(); cargo_types = db.query(models.CargoType).all(); polygons = db.query(models.Polygon).all(); tariffs = db.query(models.Tariff).all()
    last = db.query(models.TripRequest).filter(models.TripRequest.kind == TripType.SAMOSVAL, models.TripRequest.number.like("С-%")).order_by(models.TripRequest.id.desc()).first()
    next_num = 1
    if last and last.number:
        try:
            next_num = int(str(last.number).split("-", 1)[1]) + 1
        except Exception:
            next_num = 1
    return render_template("request_form.html", {"request": request, "user": current_user, "menu": menu, "vtypes": vtypes, "vehicles": vehicles, "drivers": drivers, "customers": customers, "cargo_types": cargo_types, "polygons": polygons, "tariffs": tariffs, "kind": "самосвал", "next_number": f"С-{next_num}", "app_name": "ГРАУНД | Рейсы"})

@app.post("/requests/new")
def create_request(request: Request, number: Optional[str] = Form(None), planned_date: str = Form(...), planned_time: str = Form(""), driver_id: Optional[str] = Form(None), vehicle_id: Optional[str] = Form(None), load_address: str = Form(""), unload_address: str = Form(""), route_name: str = Form(""), km: Optional[str] = Form("0"), volume: Optional[str] = Form("0"), trips_count: Optional[str] = Form("1"), cargo_type_id: Optional[str] = Form(None), customer_id: Optional[str] = Form(None), customer_name_manual: Optional[str] = Form(None), polygon_id: Optional[str] = Form(None), waste_bin_count: Optional[str] = Form(None), kind: str = Form(...), comment: str = Form(""), tariff_id: Optional[str] = Form(None), current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    resolved_customer_id = None
    if customer_id:
        resolved_customer_id = int(customer_id)
    elif customer_name_manual and customer_name_manual.strip():
        cust = db.query(models.Customer).filter(models.Customer.name == customer_name_manual.strip()).first()
        if not cust:
            cust = models.Customer(name=customer_name_manual.strip(), address="")
            db.add(cust); db.flush()
        resolved_customer_id = cust.id
    req = models.TripRequest(number=number, planned_date=date.fromisoformat(planned_date), planned_time=planned_time, driver_id=int(driver_id) if driver_id else None, vehicle_id=int(vehicle_id) if vehicle_id else None, load_address=load_address, unload_address=unload_address, route_name=route_name, km=float(km or 0), volume=float(volume or 0), trips_count=int(trips_count or 1), cargo_type_id=int(cargo_type_id) if cargo_type_id else None, customer_id=resolved_customer_id, polygon_id=int(polygon_id) if polygon_id else None, waste_bin_count=int(waste_bin_count) if waste_bin_count not in (None, "", "0") else None, kind=TripType(kind), status=RequestStatus.ASSIGNED if driver_id else RequestStatus.NEW, comment=comment)
    if not req.driver_id:
        raise HTTPException(status_code=400, detail="Не выбран водитель")
    db.add(req); db.flush()
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=None, new_status=RequestStatus.NEW.value))
    if not req.number or not str(req.number).strip():
        prefix = "П" if req.kind == TripType.PUKHTOVOZ else "С"
        last = db.query(models.TripRequest).filter(models.TripRequest.kind == req.kind, models.TripRequest.number.like(prefix + "-%")).order_by(models.TripRequest.id.desc()).first()
        next_num = 1
        if last and last.number:
            try:
                next_num = int(str(last.number).split("-", 1)[1]) + 1
            except Exception:
                next_num = 1
        req.number = f"{prefix}-{next_num}"
    tariff = None
    if tariff_id:
        tariff = db.query(models.Tariff).filter(models.Tariff.id == int(tariff_id)).first()
    if not tariff:
        tariff = db.query(models.Tariff).filter(models.Tariff.kind == req.kind, models.Tariff.is_active == True, models.Tariff.min_km <= (req.km or 0), models.Tariff.max_km >= (req.km or 0)).first()
    req.tariff_id = tariff.id if tariff else None
    if tariff:
        req.sum_driver = 0
        req.sum_driver += (req.km or 0) * (tariff.km_price or 0)
        req.sum_driver += (req.volume or 0) * (tariff.volume_price or 0)
        req.sum_driver += req.trips_count * (tariff.trip_price or 0)
        req.sum_driver += (tariff.fixed_sum or 0)
    req.sum_trip = req.sum_driver
    db.commit()
    return RedirectResponse("/pukhtovoz" if req.kind == TripType.PUKHTOVOZ else "/samosval", status_code=302)

@app.get("/requests/{req_id}", response_class=HTMLResponse)
def request_detail(request: Request, req_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req:
        raise HTTPException(404)
    menu = menu_for(current_user.role)
    history = db.query(models.StatusHistory).filter(models.StatusHistory.trip_request_id == req_id).order_by(models.StatusHistory.created_at.desc()).all()
    return render_template("request_detail.html", {"request": request, "user": current_user, "menu": menu, "req": req, "history": history, "app_name": "ГРАУНД | Рейсы"})

@app.post("/requests/{req_id}/accept")
def accept_trip(req_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or req.driver_id != current_user.id:
        raise HTTPException(403)
    req.status = RequestStatus.ACCEPTED
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=RequestStatus.NEW.value, new_status=RequestStatus.ACCEPTED.value))
    db.commit()
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.post("/requests/{req_id}/start")
def start_trip(req_id: int, actual_km: Optional[str] = Form("0"), actual_volume: Optional[str] = Form("0"), current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or req.driver_id != current_user.id:
        raise HTTPException(403)
    req.status = RequestStatus.IN_WORK
    req.started_at = datetime.utcnow()
    req.actual_km = float(actual_km or 0)
    req.actual_volume = float(actual_volume or 0)
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=RequestStatus.ACCEPTED.value, new_status=RequestStatus.IN_WORK.value))
    db.commit()
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.post("/requests/{req_id}/complete")
def complete_trip(req_id: int, actual_km: Optional[str] = Form("0"), actual_volume: Optional[str] = Form("0"), comment: str = Form(""), current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or req.driver_id != current_user.id:
        raise HTTPException(403)
    old = req.status
    req.status = RequestStatus.DRIVER_COMPLETED
    req.finished_at = datetime.utcnow()
    req.actual_km = float(actual_km or 0)
    req.actual_volume = float(actual_volume or 0)
    req.comment = (req.comment or "") + (" | " if req.comment else "") + comment
    tariff = db.query(models.Tariff).filter(models.Tariff.kind == req.kind, models.Tariff.is_active == True).first()
    req.tariff_id = tariff.id if tariff else req.tariff_id
    req.sum_driver = calc_sum(req, tariff)
    req.sum_trip = req.sum_driver
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=old.value, new_status=RequestStatus.DRIVER_COMPLETED.value))
    db.commit()
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.post("/requests/{req_id}/confirm")
def confirm_trip(req_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req:
        raise HTTPException(404)
    req.status = RequestStatus.LOGIST_CONFIRMED
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=RequestStatus.DRIVER_COMPLETED.value, new_status=RequestStatus.LOGIST_CONFIRMED.value))
    db.commit()
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.get("/salary", response_class=HTMLResponse)
def salary(request: Request, driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(models.TripRequest).filter(models.TripRequest.status == RequestStatus.LOGIST_CONFIRMED)
    if current_user.role == UserRole.DRIVER:
        query = query.filter(models.TripRequest.driver_id == current_user.id)
    else:
        drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
        if driver_id:
            query = query.filter(models.TripRequest.driver_id == int(driver_id))
    if date_from: query = query.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
    if date_to: query = query.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
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
            "volume": sum((r.actual_volume or r.volume or 0) for r in trips),
            "bins": sum((r.waste_bin_count or 0) for r in trips),
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
                "volume": sum((r.actual_volume or r.volume or 0) for r in trips),
                "bins": sum((r.waste_bin_count or 0) for r in trips),
            })
    menu = menu_for(current_user.role)
    return render_template("salary.html", {"request": request, "user": current_user, "menu": menu, "items": items, "drivers": drivers if current_user.role != UserRole.DRIVER else [current_user], "date_from": date_from, "date_to": date_to, "app_name": "ГРАУНД | Рейсы"})

@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request, status_f: Optional[str] = None, driver_id: Optional[str] = None, polygon_id: Optional[str] = None, customer_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    q_base = db.query(models.TripRequest)
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
        "km": sum((r.actual_km or 0) for r in rows),
        "volume": sum((r.actual_volume or r.volume or 0) for r in rows),
        "bins": sum((r.waste_bin_count or 0) for r in rows),
        "sum": sum((r.sum_driver or 0) for r in rows),
    }
    return render_template("reports.html", {"request": request, "user": current_user, "menu": menu, "summary": summary, "rows": rows, "statuses": RequestStatus, "drivers": db.query(models.User).filter(models.User.role == UserRole.DRIVER).all(), "polygons": db.query(models.Polygon).all(), "customers": db.query(models.Customer).all(), "status_f": status_f or "", "driver_id": driver_id or "", "polygon_id": polygon_id or "", "customer_id": customer_id or "", "date_from": date_from or "", "date_to": date_to or "", "q": q or "", "app_name": "ГРАУНД | Рейсы"})

@app.get("/users/new", response_class=HTMLResponse)
def new_user_form(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    menu = menu_for(current_user.role)
    return render_template("users.html", {"request": request, "user": current_user, "menu": menu, "users": db.query(models.User).all(), "editing": None, "app_name": "ГРАУНД | Рейсы"})

@app.post("/users/new")
def create_user(full_name: str = Form(...), login: str = Form(...), password: str = Form(...), role: str = Form(...), current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.login == login).first():
        return RedirectResponse("/users?error=login_exists", status_code=302)
    u = models.User(full_name=full_name, login=login, password_hash=pwd_hash(password), role=UserRole(role), is_active=True)
    db.add(u); db.commit()
    return RedirectResponse("/users", status_code=302)

@app.post("/users/{user_id}/delete")
def delete_user(user_id: int, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or user.id == current_user.id:
        return RedirectResponse("/users", status_code=302)
    db.query(models.StatusHistory).filter(models.StatusHistory.user_id == user.id).update({"user_id": None}, synchronize_session=False)
    db.delete(user)
    db.commit()
    return RedirectResponse("/users", status_code=302)

@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("access_token")
    return resp

@app.get("/export/requests.csv")
def export_csv(status_f: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.TripRequest)
    if status_f: q = q.filter(models.TripRequest.status == RequestStatus(status_f))
    rows = q.all()
    out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(["Номер", "Дата", "Статус", "Водитель", "Автомобиль", "Сумма водителю"])
    for r in rows:
        writer.writerow([r.number, r.planned_date, r.status.value, r.driver.full_name if r.driver else "", r.vehicle.name if r.vehicle else "", r.sum_driver or 0])
    return Response(content=out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=requests.csv"})

@app.get("/export/salary.xlsx")
def export_xlsx(db: Session = Depends(get_db)):
    drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all(); wb = Workbook(); ws = wb.active; ws.append(["Водитель", "Кол-во заявок", "Километраж", "Объем", "Сумма"])
    for d in drivers:
        trips = db.query(models.TripRequest).filter(models.TripRequest.driver_id == d.id, models.TripRequest.status == RequestStatus.LOGIST_CONFIRMED).all()
        ws.append([d.full_name, len(trips), sum(t.actual_km or 0 for t in trips), sum(t.actual_volume or 0 for t in trips), sum(t.sum_driver or 0 for t in trips)])
    path = os.path.join(root_dir, "uploads", "salary.xlsx"); wb.save(path)
    return FileResponse(path, filename="salary.xlsx")

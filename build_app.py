import os
root = r"C:\Users\PC\Desktop\ground-reis"
app_path = os.path.join(root, "app.py")
with open(app_path, "w", encoding="utf-8") as f:
    f.write('''import os, io, json, csv
from datetime import datetime, timedelta, date
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from jose import jwt
from passlib.context import CryptContext
from openpyxl import Workbook
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from dotenv import load_dotenv

from backend import models, auth
from backend.database import SessionLocal, get_db, engine
from backend.auth import get_password_hash, verify_password, create_access_token, get_current_user, require_role
from backend.models import UserRole, RequestStatus, TripType, CalcStatus

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY", "ground_secret_key_2026")
ALGORITHM = "HS256"

app = FastAPI(title="GRUND | Рейсы")
app.mount("/static", StaticFiles(directory=os.path.join(root, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(root, "templates"))
UPLOAD_DIR = os.path.join(root, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

def get_db_dep():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Ensure tables
models.Base.metadata.create_all(bind=engine)

# Seed
def seed(db: Session):
    if db.query(models.User).count() == 0:
        admin = models.User(full_name="Администратор", login="admin", password_hash=get_password_hash("admin123"), role=UserRole.ADMIN, is_active=True)
        logist = models.User(full_name="Логист Иван", login="logist", password_hash=get_password_hash("logist123"), role=UserRole.LOGIST, is_active=True)
        driver1 = models.User(full_name="Петров Петр", login="driver1", password_hash=get_password_hash("driver123"), role=UserRole.DRIVER, is_active=True)
        driver2 = models.User(full_name="Сидоров Алексей", login="driver2", password_hash=get_password_hash("driver123"), role=UserRole.DRIVER, is_active=True)
        db.add_all([admin, logist, driver1, driver2])
        db.flush()
        vt1 = models.VehicleType(name="КАМАЗ пухтовоз", kind=TripType.PUKHTOVOZ)
        vt2 = models.VehicleType(name="Урал самосвал", kind=TripType.SAMOSVAL)
        db.add_all([vt1, vt2])
        db.flush()
        v1 = models.Vehicle(name="КАМАЗ-65115", plate="А 123 БС 78", type_id=vt1.id, capacity=12)
        v2 = models.Vehicle(name="Урал-6560", plate="В 456 КТ 99", type_id=vt2.id, capacity=20)
        db.add_all([v1, v2])
        db.flush()
        cust = models.Customer(name="ООО СтройГарант", address="СПб, пр. Просвещения, 12")
        db.add(cust)
        db.flush()
        cargo = models.CargoType(name="Строймусор", unit="м3")
        db.add(cargo)
        db.flush()
        t1 = models.Tariff(title="Пухтовоз до 15км", vehicle_type_id=vt1.id, kind=TripType.PUKHTOVOZ, min_km=0, max_km=15, trip_price=3500, formula="trip")
        t2 = models.Tariff(title="Самосвал до 15км", vehicle_type_id=vt2.id, kind=TripType.SAMOSVAL, min_km=0, max_km=15, trip_price=2800, formula="trip")
        db.add_all([t1, t2])
        for i in range(1,4):
            req = models.TripRequest(number=f"П-2026-{i:03d}", planned_date=date.today() - timedelta(days=i), planned_time="08:00" if i%2 else "14:00", driver_id=driver1.id if i%2 else driver2.id, vehicle_id=v1.id if i%2 else v2.id, load_address="СПб, ул. Строителей, 1", unload_address="Полигон Ленинградская обл.", route_name="Маршрут А", km=12+i, volume=8+i, trips_count=1, cargo_type_id=cargo.id, customer_id=cust.id, kind=TripType.PUKHTOVOZ if i%2 else TripType.SAMOSVAL, status=RequestStatus.LOGIST_CONFIRMED, actual_km=12+i, actual_volume=8+i, sum_trip=3500+i*200, sum_driver=3500+i*200, tariff_id=t1.id if i%2 else t2.id, logist_comment="Без замечаний")
            db.add(req)
            db.flush()
            hist = models.StatusHistory(trip_request_id=req.id, user_id=logist.id, old_status=RequestStatus.NEW.value, new_status=RequestStatus.LOGIST_CONFIRMED.value)
            db.add(hist)
        db.commit()
    db.close()

with SessionLocal() as db:
    seed(db)

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "app_name": "ГРАУНД | Рейсы"})

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "app_name": "ГРАУНД | Рейсы"})

@app.post("/auth/login")
def login_api(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db_dep)):
    user = db.query(models.User).filter(models.User.login == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль")
    token = create_access_token(data={"sub": str(user.id), "role": str(user.role)})
    return {"access_token": token, "token_type": "bearer", "role": str(user.role), "user_id": user.id, "full_name": user.full_name}

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db_dep)):
    stats = {"requests_today": 0, "new_requests": 0, "in_work": 0, "pending_review": 0, "delayed": 0, "sum_today": 0}
    if current_user.role == UserRole.DRIVER:
        reqs = db.query(models.TripRequest).filter(models.TripRequest.driver_id == current_user.id).order_by(models.TripRequest.planned_date.desc()).limit(5).all()
        today = db.query(models.TripRequest).filter(models.TripRequest.driver_id == current_user.id, models.TripRequest.planned_date == date.today()).all()
        stats["requests_today"] = len(today)
        stats["sum_today"] = sum([x.sum_driver or 0 for x in today])
        stats["active"] = [r for r in today if r.status in (RequestStatus.IN_WORK, RequestStatus.ACCEPTED)]
    else:
        reqs = db.query(models.TripRequest).order_by(models.TripRequest.planned_date.desc()).limit(5).all()
        stats["requests_today"] = db.query(models.TripRequest).filter(models.TripRequest.planned_date == date.today()).count()
        stats["new_requests"] = db.query(models.TripRequest).filter(models.TripRequest.status == RequestStatus.NEW).count()
        stats["in_work"] = db.query(models.TripRequest).filter(models.TripRequest.status == RequestStatus.IN_WORK).count()
        stats["pending_review"] = db.query(models.TripRequest).filter(models.TripRequest.status == RequestStatus.ON_REVIEW).count()
    menu = menu_for(current_user.role)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": current_user, "menu": menu, "stats": stats, "reqs": reqs, "app_name": "ГРАУНД | Рейсы"})

@app.get("/requests", response_class=HTMLResponse)
def requests_page(request: Request, status: Optional[str] = None, kind: Optional[str] = None, driver_id: Optional[int] = None, q: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db_dep)):
    rs = db.query(models.TripRequest)
    if current_user.role == UserRole.DRIVER:
        rs = rs.filter(models.TripRequest.driver_id == current_user.id)
    if status:
        rs = rs.filter(models.TripRequest.status == RequestStatus(status))
    if kind:
        rs = rs.filter(models.TripRequest.kind == TripType(kind))
    if driver_id and current_user.role != UserRole.DRIVER:
        rs = rs.filter(models.TripRequest.driver_id == driver_id)
    if q:
        rs = rs.filter(models.TripRequest.number.contains(q))
    rs = rs.order_by(models.TripRequest.planned_date.desc()).all()
    menu = menu_for(current_user.role)
    drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
    return templates.TemplateResponse("requests.html", {"request": request, "user": current_user, "menu": menu, "reqs": rs, "drivers": drivers, "app_name": "ГРАУНД | Рейсы"})

@app.get("/requests/new", response_class=HTMLResponse)
def new_request_form(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db_dep)):
    vtypes = db.query(models.VehicleType).all()
    vehicles = db.query(models.Vehicle).all()
    drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
    customers = db.query(models.Customer).all()
    cargo_types = db.query(models.CargoType).all()
    menu = menu_for(current_user.role)
    return templates.TemplateResponse("request_form.html", {"request": request, "user": current_user, "menu": menu, "vtypes": vtypes, "vehicles": vehicles, "drivers": drivers, "customers": customers, "cargo_types": cargo_types, "app_name": "ГРАУНД | Рейсы"})

@app.post("/requests")
def create_request(request: Request, number: str = Form(...), planned_date: str = Form(...), planned_time: str = Form(""), driver_id: Optional[str] = Form(None), vehicle_id: Optional[str] = Form(None), load_address: str = Form(""), unload_address: str = Form(""), route_name: str = Form(""), km: Optional[str] = Form("0"), volume: Optional[str] = Form("0"), trips_count: Optional[str] = Form("1"), cargo_type_id: Optional[str] = Form(None), customer_id: Optional[str] = Form(None), kind: str = Form(...), comment: str = Form(""), current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db_dep)):
    req = models.TripRequest(number=number, planned_date=date.fromisoformat(planned_date), planned_time=planned_time, driver_id=int(driver_id) if driver_id else None, vehicle_id=int(vehicle_id) if vehicle_id else None, load_address=load_address, unload_address=unload_address, route_name=route_name, km=float(km or 0), volume=float(volume or 0), trips_count=int(trips_count or 1), cargo_type_id=int(cargo_type_id) if cargo_type_id else None, customer_id=int(customer_id) if customer_id else None, kind=TripType(kind), status=RequestStatus.NEW, comment=comment)
    db.add(req)
    db.flush()
    hist = models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=None, new_status=RequestStatus.NEW.value)
    db.add(hist)
    db.commit()
    return RedirectResponse("/requests", status_code=302)

@app.post("/requests/{req_id}/status")
def set_status(req_id: int, status: str = Form(...), current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db_dep)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req:
        raise HTTPException(404)
    old = req.status
    req.status = RequestStatus(status)
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=old.value if old else None, new_status=RequestStatus(status).value))
    db.commit()
    return RedirectResponse("/requests", status_code=302)

@app.get("/requests/{req_id}", response_class=HTMLResponse)
def request_detail(request: Request, req_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db_dep)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req:
        raise HTTPException(404)
    menu = menu_for(current_user.role)
    history = db.query(models.StatusHistory).filter(models.StatusHistory.trip_request_id == req_id).order_by(models.StatusHistory.created_at.desc()).all()
    return templates.TemplateResponse("request_detail.html", {"request": request, "user": current_user, "menu": menu, "req": req, "history": history, "app_name": "ГРАУНД | Рейсы"})

@app.post("/requests/{req_id}/start")
def start_trip(req_id: int, actual_km: Optional[str] = Form(None), actual_volume: Optional[str] = Form(None), current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db_dep)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or req.driver_id != current_user.id:
        raise HTTPException(403)
    req.status = RequestStatus.IN_WORK
    req.started_at = datetime.utcnow()
    if actual_km: req.actual_km = float(actual_km)
    if actual_volume: req.actual_volume = float(actual_volume)
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=RequestStatus.ACCEPTED.value, new_status=RequestStatus.IN_WORK.value))
    db.commit()
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.post("/requests/{req_id}/complete")
def complete_trip(req_id: int, actual_km: Optional[str] = Form("0"), actual_volume: Optional[str] = Form("0"), comment: str = Form(""), current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db_dep)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or req.driver_id != current_user.id:
        raise HTTPException(403)
    old = req.status
    req.status = RequestStatus.DRIVER_COMPLETED
    req.finished_at = datetime.utcnow()
    req.actual_km = float(actual_km or 0)
    req.actual_volume = float(actual_volume or 0)
    req.comment = comment
    tariff = db.query(models.Tariff).filter(models.Tariff.kind == req.kind, models.Tariff.is_active == True).first()
    if tariff:
        req.tariff_id = tariff.id
        req.sum_driver = calc_sum(req, tariff)
    req.sum_trip = req.sum_driver
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=old.value, new_status=RequestStatus.DRIVER_COMPLETED.value))
    db.commit()
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.post("/requests/{req_id}/accept")
def accept(req_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db_dep)):
    req = db.query(models.TripRequest).filter(models.TripRequest.id == req_id).first()
    if not req or req.driver_id != current_user.id:
        raise HTTPException(403)
    old = req.status
    req.status = RequestStatus.ACCEPTED
    db.add(models.StatusHistory(trip_request_id=req.id, user_id=current_user.id, old_status=old.value if old else None, new_status=RequestStatus.ACCEPTED.value))
    db.commit()
    return RedirectResponse(f"/requests/{req_id}", status_code=302)

@app.get("/salary", response_class=HTMLResponse)
def salary_page(request: Request, driver_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db_dep)):
    if current_user.role == UserRole.DRIVER:
        drivers = [current_user]
    else:
        drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
    items = []
    ids = [d.id for d in drivers]
    for did in ids:
        user = db.query(models.User).filter(models.User.id == did).first()
        q = db.query(models.TripRequest).filter(models.TripRequest.driver_id == did, models.TripRequest.status == RequestStatus.LOGIST_CONFIRMED)
        if date_from:
            q = q.filter(models.TripRequest.planned_date >= date.fromisoformat(date_from))
        if date_to:
            q = q.filter(models.TripRequest.planned_date <= date.fromisoformat(date_to))
        rows = q.all()
        items.append({"driver": user, "trips": rows, "total": sum(r.sum_driver or 0 for r in rows)})
    menu = menu_for(current_user.role)
    return templates.TemplateResponse("salary.html", {"request": request, "user": current_user, "menu": menu, "items": items, "drivers": drivers, "date_from": date_from, "date_to": date_to, "app_name": "ГРАУНД | Рейсы"})

@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db_dep)):
    menu = menu_for(current_user.role)
    return templates.TemplateResponse("reports.html", {"request": request, "user": current_user, "menu": menu, "app_name": "ГРАУНД | Рейсы"})

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN, UserRole.LOGIST)), db: Session = Depends(get_db_dep)):
    menu = menu_for(current_user.role)
    tariffs = db.query(models.Tariff).all()
    vehicles = db.query(models.Vehicle).all()
    vtypes = db.query(models.VehicleType).all()
    customers = db.query(models.Customer).all()
    cargo_types = db.query(models.CargoType).all()
    return templates.TemplateResponse("settings.html", {"request": request, "user": current_user, "menu": menu, "tariffs": tariffs, "vehicles": vehicles, "vtypes": vtypes, "customers": customers, "cargo_types": cargo_types, "app_name": "ГРАУНД | Рейсы"})

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, current_user: models.User = Depends(require_role(UserRole.ADMIN)), db: Session = Depends(get_db_dep)):
    users = db.query(models.User).all()
    menu = menu_for(current_user.role)
    return templates.TemplateResponse("users.html", {"request": request, "user": current_user, "menu": menu, "users": users, "app_name": "ГРАУНД | Рейсы"})

@app.get("/logout")
def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie("access_token")
    return resp

@app.get("/export/requests.csv")
def export_csv(status: Optional[str] = None, db: Session = Depends(get_db_dep)):
    q = db.query(models.TripRequest)
    if status:
        q = q.filter(models.TripRequest.status == RequestStatus(status))
    rows = q.all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Номер", "Дата", "Статус", "Водитель", "Автомобиль", "Сумма водителю"])
    for r in rows:
        writer.writerow([r.number, r.planned_date, r.status.value, r.driver.full_name if r.driver else "", r.vehicle.name if r.vehicle else "", r.sum_driver or 0])
    return Response(content=out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=requests.csv"})

@app.get("/export/salary.xlsx")
def export_xlsx(db: Session = Depends(get_db_dep)):
    drivers = db.query(models.User).filter(models.User.role == UserRole.DRIVER).all()
    wb = Workbook()
    ws = wb.active
    ws.append(["Водитель", "Кол-во заявок", "Километраж", "Объем", "Сумма"])
    for d in drivers:
        trips = db.query(models.TripRequest).filter(models.TripRequest.driver_id == d.id, models.TripRequest.status == RequestStatus.LOGIST_CONFIRMED).all()
        ws.append([d.full_name, len(trips), sum(t.actual_km or 0 for t in trips), sum(t.actual_volume or 0 for t in trips), sum(t.sum_driver or 0 for t in trips)])
    path = os.path.join(root, "uploads", "salary.xlsx")
    wb.save(path)
    return FileResponse(path, filename="salary.xlsx")

from starlette.responses import Response

def calc_sum(req: models.TripRequest, tariff: models.Tariff):
    trips = req.trips_count or 1
    if tariff.formula == "km":
        return (req.actual_km or req.km or 0) * (tariff.km_price or 0)
    if tariff.formula == "volume":
        return (req.actual_volume or req.volume or 0) * (tariff.volume_price or 0)
    if tariff.formula == "fixed":
        return tariff.fixed_sum or 0
    return trips * (tariff.trip_price or 0)

def menu_for(role: str):
    m = [
        {"href": "/dashboard", "label": "Главная", "icon": "🏠"},
        {"href": "/requests", "label": "Заявки", "icon": "📋"},
        {"href": "/reports", "label": "Отчеты", "icon": "📊"},
        {"href": "/settings", "label": "Настройки", "icon": "⚙️"},
        {"href": "/salary", "label": "Зарплата", "icon": "💰"},
    ]
    if role == UserRole.ADMIN:
        m.append({"href": "/users", "label": "Пользователи", "icon": "👥"})
    return m
''')
print("app.py written")

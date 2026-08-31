from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey, Enum, Date, LargeBinary, UniqueConstraint, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from enum import Enum as PyEnum
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///ground.db")
_engine = None

def _build_engine(url: str):
    from sqlalchemy import create_engine
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # Внешняя БД запускается fail-closed: скрытая запись в локальную SQLite недопустима.
    return create_engine(url, pool_pre_ping=True)

engine = _build_engine(DATABASE_URL)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

class UserRole(str, PyEnum):
    ADMIN = "admin"
    LOGIST = "logist"
    DRIVER = "driver"

class RequestStatus(str, PyEnum):
    NEW = "Новая"
    ASSIGNED = "Назначена водителю"
    ACCEPTED = "Принята водителем"
    IN_WORK = "В работе"
    DRIVER_COMPLETED = "Завершена водителем"
    ON_REVIEW = "На проверке"
    LOGIST_CONFIRMED = "Подтверждена логистом"
    NEEDS_CORRECTION = "Требует исправления"
    CANCELLED = "Отменена"

class TripType(str, PyEnum):
    PUKHTOVOZ = "пухтовоз"
    SAMOSVAL = "самосвал"

class CalcStatus(str, PyEnum):
    DRAFT = "Черновик"
    ON_REVIEW = "На проверке"
    CONFIRMED = "Подтвержден"
    PAID = "Выплачен"
    CORRECTED = "Скорректирован"

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String(255), nullable=False)
    login = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), nullable=False)
    phone = Column(String(50))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class VehicleType(Base):
    __tablename__ = "vehicle_types"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True)
    kind = Column(Enum(TripType), nullable=False)
    description = Column(Text)

class Vehicle(Base):
    __tablename__ = "vehicles"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    plate = Column(String(50), unique=True, nullable=False)
    type_id = Column(Integer, ForeignKey("vehicle_types.id"), nullable=False)
    capacity = Column(Float)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    type = relationship("VehicleType")

class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    inn = Column(String(12), nullable=True, index=True)
    bitrix_company_id = Column(Integer, nullable=True, unique=True, index=True)
    bitrix_contact_id = Column(Integer, nullable=True, index=True)
    contact = Column(String(255))
    phone = Column(String(50))
    address = Column(Text)
    comment = Column(Text)

class CargoType(Base):
    __tablename__ = "cargo_types"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    unit = Column(String(50))
    comment = Column(Text)
    polygon_tariffs = relationship("PolygonTariff", back_populates="cargo_type", cascade="all, delete-orphan")

class Route(Base):
    __tablename__ = "routes"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    load_address = Column(Text)
    unload_address = Column(Text)
    distance = Column(Float)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    comment = Column(Text)
    customer = relationship("Customer")

class Tariff(Base):
    __tablename__ = "tariffs"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    vehicle_type_id = Column(Integer, ForeignKey("vehicle_types.id"), nullable=False)
    kind = Column(Enum(TripType), nullable=False)
    min_km = Column(Float, default=0)
    max_km = Column(Float, nullable=True)
    min_volume = Column(Float, default=0)
    max_volume = Column(Float, nullable=True)
    trip_price = Column(Float, default=0)
    km_price = Column(Float, default=0)
    volume_price = Column(Float, default=0)
    fixed_sum = Column(Float, default=0)
    extra_fee = Column(Float, default=0)
    coefficient = Column(Float, default=1.0)
    formula = Column(String(50), default="trip")
    date_from = Column(Date, nullable=True)
    date_to = Column(Date, nullable=True)
    is_active = Column(Boolean, default=True)
    comment = Column(Text)
    vehicle_type = relationship("VehicleType")

class TripRequest(Base):
    __tablename__ = "trip_requests"
    id = Column(Integer, primary_key=True, index=True)
    number = Column(String(255), nullable=False, unique=True)
    planned_date = Column(Date, nullable=False)
    planned_time = Column(String(50), nullable=True)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=True)
    load_address = Column(Text)
    unload_address = Column(Text)
    route_name = Column(String(255))
    km = Column(Float, default=0)
    volume = Column(Float, default=0)
    tonnage = Column(Float, nullable=True)
    trips_count = Column(Integer, default=1)
    cargo_type_id = Column(Integer, ForeignKey("cargo_types.id"), nullable=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    kind = Column(Enum(TripType), nullable=False)
    status = Column(Enum(RequestStatus), default=RequestStatus.NEW)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    actual_km = Column(Float, nullable=True)
    actual_volume = Column(Float, nullable=True)
    actual_tonnage = Column(Float, nullable=True)
    is_empty_run = Column(Boolean, default=False)
    empty_run_comment = Column(Text, nullable=True)
    has_downtime = Column(Boolean, default=False)
    downtime_minutes = Column(Integer, nullable=True)
    downtime_comment = Column(Text, nullable=True)
    polygon_cost_manual = Column(Float, nullable=True)
    sum_trip = Column(Float, nullable=True)
    sum_driver = Column(Float, nullable=True)
    tariff_id = Column(Integer, ForeignKey("tariffs.id"), nullable=True)
    # Для самосвалов менеджер Bitrix выбирает отдельное поле «Базовая ставка».
    # Храним выбранное название независимо от tariff_id, чтобы значение из CRM
    # не терялось даже до настройки цены для конкретного объёма.
    base_rate = Column(String(255), nullable=True)
    comment = Column(Text)
    logist_comment = Column(Text)
    site_contact_name = Column(String(255), nullable=True)
    site_contact_phone = Column(String(50), nullable=True)
    site_contact_comment = Column(Text, nullable=True)
    polygon_id = Column(Integer, ForeignKey("polygons.id"), nullable=True)
    polygon_rate_snapshot = Column(Float, nullable=True)
    polygon_unit_snapshot = Column(String(10), nullable=True)
    waste_bin_count = Column(Integer, nullable=True)
    bitrix_element_id = Column(Integer, nullable=True, index=True)
    bitrix_entity_type_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    driver = relationship("User")
    vehicle = relationship("Vehicle")
    cargo_type = relationship("CargoType")
    customer = relationship("Customer")
    tariff = relationship("Tariff")
    polygon = relationship("Polygon")

class TripArchive(Base):
    __tablename__ = "trip_archive"
    id = Column(Integer, primary_key=True, index=True)
    origin_id = Column(Integer, nullable=False)
    number = Column(String(255), nullable=False)
    planned_date = Column(Date, nullable=False)
    planned_time = Column(String(50))
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=True)
    load_address = Column(Text)
    unload_address = Column(Text)
    route_name = Column(String(255))
    km = Column(Float, default=0)
    volume = Column(Float, default=0)
    trips_count = Column(Integer, default=1)
    cargo_type_id = Column(Integer, ForeignKey("cargo_types.id"), nullable=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    kind = Column(Enum(TripType), nullable=False)
    status = Column(String(255), nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    actual_km = Column(Float, nullable=True)
    actual_volume = Column(Float, nullable=True)
    sum_trip = Column(Float, nullable=True)
    sum_driver = Column(Float, nullable=True)
    tariff_id = Column(Integer, ForeignKey("tariffs.id"), nullable=True)
    comment = Column(Text)
    logist_comment = Column(Text)
    polygon_id = Column(Integer, ForeignKey("polygons.id"), nullable=True)
    waste_bin_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    archived_at = Column(DateTime, default=datetime.utcnow)
    driver = relationship("User")
    vehicle = relationship("Vehicle")
    cargo_type = relationship("CargoType")
    customer = relationship("Customer")
    tariff = relationship("Tariff")
    polygon = relationship("Polygon")

class StatusHistory(Base):
    __tablename__ = "status_history"
    id = Column(Integer, primary_key=True, index=True)
    trip_request_id = Column(Integer, ForeignKey("trip_requests.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    old_status = Column(String(255))
    new_status = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    note = Column(Text)
    trip_request = relationship("TripRequest")
    user = relationship("User")

class SalaryCalc(Base):
    __tablename__ = "salary_calcs"
    id = Column(Integer, primary_key=True, index=True)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date_from = Column(Date, nullable=False)
    date_to = Column(Date, nullable=False)
    trips_count = Column(Integer, default=0)
    km = Column(Float, default=0)
    volume = Column(Float, default=0)
    total_driver_sum = Column(Float, default=0)
    adjustment = Column(Float, default=0)
    deduction = Column(Float, default=0)
    total = Column(Float, default=0)
    status = Column(Enum(CalcStatus), default=CalcStatus.DRAFT)
    comment = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    driver = relationship("User")

class SalaryCalcItem(Base):
    __tablename__ = "salary_calc_items"
    id = Column(Integer, primary_key=True, index=True)
    salary_calc_id = Column(Integer, ForeignKey("salary_calcs.id"), nullable=False)
    trip_request_id = Column(Integer, ForeignKey("trip_requests.id"), nullable=False, unique=True)
    sum = Column(Float, nullable=False)
    salary_calc = relationship("SalaryCalc")
    trip_request = relationship("TripRequest")

class Attachment(Base):
    __tablename__ = "attachments"
    id = Column(Integer, primary_key=True, index=True)
    trip_request_id = Column(Integer, ForeignKey("trip_requests.id"), nullable=False)
    filename = Column(String(512), nullable=False)
    content_type = Column(String(100))
    size = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
    path = Column(String(1024))
    content = Column(LargeBinary, nullable=True)
    bitrix_file_id = Column(Integer, nullable=True, index=True)
    trip_request = relationship("TripRequest")


class DriverDayReport(Base):
    __tablename__ = "driver_day_reports"
    __table_args__ = (UniqueConstraint("driver_id", "report_date", name="uq_driver_day_report_date"),)
    id = Column(Integer, primary_key=True, index=True)
    report_date = Column(Date, nullable=False)
    driver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False)
    total_km = Column(Float, default=0, nullable=False)
    odometer = Column(Float, default=0, nullable=False)
    fuel_liters = Column(Float, default=0, nullable=False)
    fuel_price = Column(Float, default=0, nullable=False)
    comment = Column(Text)

    @property
    def fuel_cost(self):
        return float(self.fuel_liters or 0) * float(self.fuel_price or 0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    driver = relationship("User")
    vehicle = relationship("Vehicle")

class Polygon(Base):
    __tablename__ = "polygons"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    address = Column(Text)
    contact = Column(String(255))
    phone = Column(String(50))
    comment = Column(Text)
    entry_notes = Column(Text)
    navigator_url = Column(String(1024))
    calculation_method = Column(String(20), default="volume")
    volume_rate = Column(Float, default=0)
    tonnage_rate = Column(Float, default=0)
    waste_types = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    tariffs = relationship("PolygonTariff", back_populates="polygon", cascade="all, delete-orphan")


class PolygonTariff(Base):
    __tablename__ = "polygon_tariffs"
    __table_args__ = (UniqueConstraint("polygon_id", "cargo_type_id", name="uq_polygon_cargo_tariff"),)
    id = Column(Integer, primary_key=True, index=True)
    polygon_id = Column(Integer, ForeignKey("polygons.id"), nullable=False, index=True)
    cargo_type_id = Column(Integer, ForeignKey("cargo_types.id"), nullable=False, index=True)
    rate = Column(Float, nullable=False, default=0)
    unit = Column(String(10), nullable=False, default="м³")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    polygon = relationship("Polygon", back_populates="tariffs")
    cargo_type = relationship("CargoType", back_populates="polygon_tariffs")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(255))
    section = Column(String(255))
    record_id = Column(Integer, nullable=True)
    old_value = Column(Text)
    new_value = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User")

class IntegrationSetting(Base):
    __tablename__ = "integration_settings"
    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(100), unique=True, nullable=False)
    webhook_url = Column(String(500))
    secret = Column(String(200))
    responsible_id = Column(String(100))
    sync_direction = Column(String(100), default="two_way")
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


@event.listens_for(User.__table__, "after_create")
def _sqlite_last_admin_triggers(_target, connection, **_kwargs):
    if connection.dialect.name != "sqlite":
        return
    connection.exec_driver_sql("""
        CREATE TRIGGER IF NOT EXISTS protect_last_active_admin_update
        BEFORE UPDATE OF role, is_active ON users
        WHEN OLD.role = 'ADMIN' AND OLD.is_active = 1
          AND (NEW.role <> 'ADMIN' OR NEW.is_active <> 1)
          AND (SELECT COUNT(*) FROM users WHERE role = 'ADMIN' AND is_active = 1 AND id <> OLD.id) = 0
        BEGIN SELECT RAISE(ABORT, 'last active admin'); END
    """)
    connection.exec_driver_sql("""
        CREATE TRIGGER IF NOT EXISTS protect_last_active_admin_delete
        BEFORE DELETE ON users
        WHEN OLD.role = 'ADMIN' AND OLD.is_active = 1
          AND (SELECT COUNT(*) FROM users WHERE role = 'ADMIN' AND is_active = 1 AND id <> OLD.id) = 0
        BEGIN SELECT RAISE(ABORT, 'last active admin'); END
    """)

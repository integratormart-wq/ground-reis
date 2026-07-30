from sqlalchemy.orm import Session
from datetime import datetime
from . import models, auth
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/routes", tags=["routes"])

class RouteCreate(BaseModel):
    name: str
    load_address: Optional[str] = None
    unload_address: Optional[str] = None
    distance: Optional[float] = None
    customer_id: Optional[int] = None
    comment: Optional[str] = None

class RouteRead(BaseModel):
    id: int
    name: str
    load_address: Optional[str]
    unload_address: Optional[str]
    distance: Optional[float]
    customer_id: Optional[int]
    comment: Optional[str]

@router.get("/", response_model=list[RouteRead])
def list_routes(skip: int = 0, limit: int = 100, db: Session = Depends(auth.get_db)):
    return db.query(models.Route).offset(skip).limit(limit).all()

@router.post("/", response_model=RouteRead)
def create_route(route: RouteCreate, current_user: models.User = Depends(auth.require_role(models.UserRole.ADMIN, models.UserRole.LOGIST)), db: Session = Depends(auth.get_db)):
    r = models.Route(**route.dict())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r

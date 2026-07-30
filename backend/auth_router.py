""" Ground Logistics Assistant - OAuth2 protected auth endpoints.
Generates: access token for users stored in users table.
"""
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from . import models, auth
from .database import get_db
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    user_id: int
    full_name: str

@router.post("/login", response_model=TokenOut)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.login == form_data.username).first()
    if not user or not auth.verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль")
    token = auth.create_access_token(data={"sub": str(user.id), "role": user.role})
    return {"access_token": token, "token_type": "bearer", "role": user.role, "user_id": user.id, "full_name": user.full_name}

@router.get("/me")
def me(current_user: models.User = Depends(auth.get_current_user)):
    return {"id": current_user.id, "full_name": current_user.full_name, "role": current_user.role, "login": current_user.login, "is_active": current_user.is_active}

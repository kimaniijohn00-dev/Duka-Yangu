"""Authentication HTTP endpoints (plan step 3).

Thin route bindings over services.auth, which holds all auth logic.
Existing endpoints are untouched; the shared require_user dependency is
not attached to them yet (scoping is the next plan step).
"""
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from services import auth as authsvc
from services.auth import require_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    phone: str
    pin: str
    shop_name: str
    invite_code: str


class LoginIn(BaseModel):
    phone: str
    pin: str


@router.post("/register")
def register(payload: RegisterIn, request: Request, response: Response):
    """Register a phone + PIN with an invite code; creates a new shop."""
    return authsvc.register(
        payload.phone, payload.pin, payload.shop_name,
        payload.invite_code, request, response,
    )


@router.post("/login")
def login(payload: LoginIn, request: Request, response: Response):
    """Log in with phone + PIN and set the session cookie."""
    return authsvc.login(payload.phone, payload.pin, request, response)


@router.post("/logout")
def logout(request: Request, response: Response):
    """Revoke the current session server-side and clear the cookie."""
    return authsvc.logout(request, response)


@router.get("/me")
def me(request: Request):
    """The logged-in user's shop name and id. Never the PIN hash."""
    user = require_user(request)
    return {
        "user_id": user["user_id"],
        "shop_id": user["shop_id"],
        "shop_name": user["shop_name"],
    }

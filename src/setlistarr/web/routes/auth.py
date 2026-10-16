from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import auth
from ..deps import get_conn, templates

router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str = "/", conn: sqlite3.Connection = Depends(get_conn)):
    if not auth.has_user(conn) and not auth.auth_disabled():
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": auth.safe_next(next), "error": None})


@router.post("/login")
async def login(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    username, password = str(form.get("username", "")), str(form.get("password", ""))
    nxt = auth.safe_next(str(form.get("next", "/")))
    ok = await asyncio.to_thread(auth.check_login, conn, username, password)
    if not ok:
        await asyncio.sleep(1)  # slow down guessing
        return templates.TemplateResponse(
            request, "login.html", {"next": nxt, "error": "Wrong username or password"}, status_code=401)
    request.session.clear()
    request.session["user"] = username.strip()
    return RedirectResponse(nxt, status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/setup")
def setup_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if auth.has_user(conn):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@router.post("/setup")
async def setup(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if auth.has_user(conn):  # one-shot: can't be used to take over an existing install
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    username, pw, pw2 = (str(form.get(k, "")) for k in ("username", "password", "password2"))
    error = "Passwords don't match" if pw != pw2 else None
    if not error:
        try:
            auth.create_user(conn, username, pw)
        except ValueError as e:
            error = str(e)
    if error:
        return templates.TemplateResponse(request, "setup.html", {"error": error}, status_code=400)
    request.session.clear()
    request.session["user"] = username.strip()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/password")
async def change_password(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    new, new2 = str(form.get("new", "")), str(form.get("new2", ""))
    try:
        if new != new2:
            raise ValueError("New passwords don't match")
        auth.change_password(conn, str(form.get("current", "")), new)
        msg, level = "Password changed.", "ok"
    except ValueError as e:
        msg, level = str(e), "error"
    return templates.TemplateResponse(request, "partials/alert.html", {"level": level, "message": msg})

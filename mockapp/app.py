"""A deliberately hostile stand-in for a legacy core-banking servicing console.

This is not a straw man. The properties below are the ones that actually break
automation in 2003-era enterprise web apps, and each is here for a reason:

* **Framesets.** Navigation, content and status live in separate documents.
  The same accessible name can exist in more than one frame, so a locator that
  does not record which frame it belongs to is ambiguous.
* **Table-based layout with no ``<label for>``.** Fields are labelled by the
  adjacent table cell, which is a *visual* relationship, not a markup one.
  Any targeting strategy that relies on ``<label>`` association finds nothing
  here; a correct accessible-name computation still finds the field.
* **No test IDs and meaningless class names.** ``class="c2"``, ``name="txtMbrNo"``.
  There is nothing stable to hook except role and rendered caption.
* **Real runtime states.** Validation errors, not-found, permission denial,
  interstitials, session expiry, slowness and server errors, each armable on
  demand so the error taxonomy can be demonstrated rather than described.
* **Two tenants on the same product version**, one of which reorders the
  lookup fields and renames one caption — the per-tenant drift that
  distinguishes reuse from re-recording.

Run it with ``python -m mockapp``. It binds to localhost only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mockapp.data import DEFAULT_TENANT, MEMBERS, SUB_ACCOUNT_TYPES, TENANTS
from mockapp.faults import ALL_FAULTS, BOARD

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="Legacy Servicing Console (mock)", docs_url=None, redoc_url=None)


def _tenant(name: str) -> dict:
    return TENANTS.get(name, TENANTS[DEFAULT_TENANT])


def _render(
    request: Request,
    template: str,
    tenant: str,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    context = {
        "tenant": tenant,
        "cfg": _tenant(tenant),
        "base": f"/t/{tenant}",
        **extra,
    }
    return TEMPLATES.TemplateResponse(request, template, context, status_code=status_code)


def _maybe_slow() -> None:
    if BOARD.consume("slow_response"):
        time.sleep(4.0)


def _expired(tenant: str) -> RedirectResponse | None:
    if BOARD.consume("session_timeout"):
        return RedirectResponse(f"/t/{tenant}/signin?reason=expired", status_code=303)
    return None


# --------------------------------------------------------------------------
# Fault switchboard (out-of-band; not part of the automated surface)
# --------------------------------------------------------------------------


@app.get("/_faults")
def faults_state() -> JSONResponse:
    return JSONResponse(BOARD.state())


@app.post("/_faults/clear")
def faults_clear() -> JSONResponse:
    BOARD.clear()
    return JSONResponse({"cleared": True})


@app.post("/_faults/{name}/arm")
def faults_arm(name: str, times: int = Query(1), sticky: bool = Query(False)) -> JSONResponse:
    if name not in ALL_FAULTS:
        return JSONResponse({"error": f"unknown fault {name!r}"}, status_code=404)
    BOARD.arm(name, times=times, sticky=sticky)
    return JSONResponse({"armed": name, "times": times, "sticky": sticky})


@app.post("/_faults/{name}/disarm")
def faults_disarm(name: str) -> JSONResponse:
    BOARD.disarm(name)
    return JSONResponse({"disarmed": name})


@app.get("/_faults/panel", response_class=HTMLResponse)
def faults_panel(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "faults.html", {"faults": BOARD.state()})


# --------------------------------------------------------------------------
# Frameset chrome
# --------------------------------------------------------------------------


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(f"/t/{DEFAULT_TENANT}/", status_code=307)


@app.get("/t/{tenant}/", response_class=HTMLResponse)
def frameset(request: Request, tenant: str) -> HTMLResponse:
    return _render(request, "frameset.html", tenant)


@app.get("/t/{tenant}/nav", response_class=HTMLResponse)
def nav(request: Request, tenant: str) -> HTMLResponse:
    return _render(request, "nav.html", tenant)


@app.get("/t/{tenant}/status", response_class=HTMLResponse)
def status_bar(request: Request, tenant: str) -> HTMLResponse:
    return _render(request, "status.html", tenant)


@app.get("/t/{tenant}/home", response_class=HTMLResponse)
def home(request: Request, tenant: str) -> HTMLResponse:
    return _render(request, "home.html", tenant)


@app.get("/t/{tenant}/signin", response_class=HTMLResponse)
def signin(request: Request, tenant: str, reason: str = "") -> HTMLResponse:
    return _render(request, "signin.html", tenant, reason=reason)


# --------------------------------------------------------------------------
# Member lookup
# --------------------------------------------------------------------------


@app.get("/t/{tenant}/lookup", response_class=HTMLResponse)
def lookup_form(request: Request, tenant: str):
    _maybe_slow()
    if (bounce := _expired(tenant)) is not None:
        return bounce
    return _render(
        request,
        "lookup.html",
        tenant,
        error=None,
        interstitial=BOARD.consume("interstitial"),
        # Simulated version drift: the same control, a different caption.
        search_caption=(
            "Submit Query" if BOARD.peek("renamed_control") else _tenant(tenant)["search_button"]
        ),
    )


@app.post("/t/{tenant}/lookup", response_class=HTMLResponse)
def lookup_submit(
    request: Request,
    tenant: str,
    txtMbrNo: str = Form(default=""),
    selBranch: str = Form(default=""),
):
    _maybe_slow()
    if (bounce := _expired(tenant)) is not None:
        return bounce

    if BOARD.consume("server_error"):
        return _render(request, "error.html", tenant, status_code=500, code="APP-5001")

    raw = (txtMbrNo or "").strip()

    def _reject(message: str) -> HTMLResponse:
        return _render(
            request,
            "lookup.html",
            tenant,
            error=message,
            submitted=raw,
            interstitial=False,
            search_caption=_tenant(tenant)["search_button"],
        )

    if BOARD.consume("validation_error"):
        return _reject("Entry rejected by host validation. Re-key and submit again.")
    if not raw:
        return _reject("Member number is required.")
    if not raw.isdigit():
        return _reject("Member number must be numeric.")

    return RedirectResponse(f"/t/{tenant}/member/{raw}", status_code=303)


@app.get("/t/{tenant}/member/{member_no}", response_class=HTMLResponse)
def member_detail(request: Request, tenant: str, member_no: str):
    _maybe_slow()
    if (bounce := _expired(tenant)) is not None:
        return bounce

    member = MEMBERS.get(member_no)

    if member is None:
        # A legitimate business answer, not an error. The mock returns HTTP 200
        # with a clear on-screen message, exactly as these applications do.
        return _render(request, "not_found.html", tenant, member_no=member_no)

    if member.restricted or BOARD.consume("permission_denied"):
        return _render(request, "denied.html", tenant, member_no=member_no)

    savings = next((a for a in member.accounts if a.kind == "REGULAR SAVINGS"), None)
    return _render(
        request,
        "member.html",
        tenant,
        member=member,
        savings=savings,
        interstitial=BOARD.consume("interstitial"),
    )


# --------------------------------------------------------------------------
# Sub-account opening — a multi-step flow with a confirmation step
# --------------------------------------------------------------------------


@app.get("/t/{tenant}/subaccount/new", response_class=HTMLResponse)
def subaccount_new(request: Request, tenant: str, member: str = Query(default="")):
    _maybe_slow()
    if (bounce := _expired(tenant)) is not None:
        return bounce
    return _render(
        request,
        "subaccount_new.html",
        tenant,
        member_no=member,
        types=SUB_ACCOUNT_TYPES,
        error=None,
    )


@app.post("/t/{tenant}/subaccount/review", response_class=HTMLResponse)
def subaccount_review(
    request: Request,
    tenant: str,
    txtMbrNo: str = Form(default=""),
    selType: str = Form(default=""),
    txtNickname: str = Form(default=""),
):
    _maybe_slow()
    if (bounce := _expired(tenant)) is not None:
        return bounce

    member = MEMBERS.get(txtMbrNo.strip())
    if member is None:
        return _render(
            request,
            "subaccount_new.html",
            tenant,
            member_no=txtMbrNo,
            types=SUB_ACCOUNT_TYPES,
            error="No member on file for that number.",
        )
    if not selType:
        return _render(
            request,
            "subaccount_new.html",
            tenant,
            member_no=txtMbrNo,
            types=SUB_ACCOUNT_TYPES,
            error="Select a sub-account type.",
        )

    label = dict(SUB_ACCOUNT_TYPES).get(selType, selType)
    return _render(
        request,
        "subaccount_review.html",
        tenant,
        member=member,
        type_code=selType,
        type_label=label,
        nickname=txtNickname.strip(),
    )


@app.post("/t/{tenant}/subaccount/confirm", response_class=HTMLResponse)
def subaccount_confirm(
    request: Request,
    tenant: str,
    txtMbrNo: str = Form(default=""),
    selType: str = Form(default=""),
):
    """The irreversible step.

    Nothing is actually created — the mock is stateless by design, so that a
    misbehaving agent cannot accumulate side effects across runs. What matters
    for the exercise is that this route is reachable only through a control
    whose caption marks it risky, and that the guardrail layer treats it
    accordingly.
    """
    _maybe_slow()
    member = MEMBERS.get(txtMbrNo.strip())
    label = dict(SUB_ACCOUNT_TYPES).get(selType, selType)
    return _render(
        request,
        "subaccount_done.html",
        tenant,
        member=member,
        new_number=f"{txtMbrNo}-{selType}",
        type_label=label,
    )

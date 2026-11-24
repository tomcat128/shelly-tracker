"""Small HTML start page: health status, devices and links to the export."""
from __future__ import annotations

from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from . import auth
from .api import fmt_time
from .db import Device, Reading, utcnow

router = APIRouter()

PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Shelly Tracker</title>
<style>
  :root {{
    --bg: #f6f7f9; --card: #fff; --text: #1d2330; --muted: #667085; --line: #e4e7ec;
    --ok: #12805c; --ok-bg: #e6f4ee; --bad: #b42318; --bad-bg: #fdecea; --link: #175cd3;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #12151b; --card: #1b1f27; --text: #e6e8ec; --muted: #98a2b3; --line: #2c323d;
      --ok: #4ade80; --ok-bg: #12301f; --bad: #f97066; --bad-bg: #3a1513; --link: #84adff;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 32px 16px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 15px; margin: 0 0 12px; color: var(--muted); font-weight: 600;
        text-transform: uppercase; letter-spacing: .04em; }}
  .sub {{ color: var(--muted); margin: 0 0 24px; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px;
           padding: 18px 20px; margin-bottom: 16px; }}
  .status {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .badge {{ display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px;
            border-radius: 999px; font-weight: 600; font-size: 13px; }}
  .badge::before {{ content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }}
  .ok {{ color: var(--ok); background: var(--ok-bg); }}
  .bad {{ color: var(--bad); background: var(--bad-bg); }}
  .muted {{ color: var(--muted); }}
  .error {{ margin-top: 10px; color: var(--bad); font-family: ui-monospace, monospace; font-size: 13px;
            overflow-wrap: anywhere; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px 10px 8px 0; border-bottom: 1px solid var(--line);
            white-space: nowrap; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 13px; }}
  tr:last-child td {{ border-bottom: 0; }}
  td.num {{ font-variant-numeric: tabular-nums; }}
  ul.links {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ font-family: ui-monospace, monospace; font-size: 13px; }}
  footer {{ color: var(--muted); font-size: 13px; margin-top: 24px; }}
  form.pin {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  form.pin input {{ font: inherit; padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px;
                   background: var(--bg); color: var(--text); width: 10em; }}
  button {{ font: inherit; padding: 6px 14px; border: 1px solid var(--line); border-radius: 6px;
           background: var(--card); color: var(--text); cursor: pointer; }}
  button.link {{ border: 0; padding: 0; background: none; color: var(--link); font-size: inherit; }}
  footer form {{ display: inline; }}
</style>
</head>
<body>
<main>
  <h1>Shelly Tracker</h1>
  <p class="sub">Daten aus der Shelly Cloud</p>

  <section class="card">
    <h2>Status</h2>
    <div class="status">
      {badge}
      <span class="muted">Letzter Abruf: {last_poll} · Intervall {interval} s · {device_count} Gerät(e)</span>
    </div>
    <div class="muted" style="margin-top:6px">{retention}
    </div>
    {error}
  </section>

  {devices}

  {export}

  {pin_form}

  <footer>Seite aktualisiert sich jede Minute · Stand {now}{logout}</footer>
</main>
</body>
</html>
"""

EXPORT = """<section class="card">
    <h2>Export</h2>
    {token_hint}
    <ul class="links">
      <li><a href="api/export">Letzte Werte</a> <span class="muted">(JSON)</span></li>
      <li><a href="api/export?from=24h">Letzte 24 Stunden</a> <span class="muted">(JSON)</span></li>
      <li><a href="api/export?from=24h&amp;format=csv-wide">Letzte 24 Stunden</a> <span class="muted">(CSV für Excel)</span></li>
      <li><a href="docs">API-Dokumentation</a> <span class="muted">(alle Parameter, zum Ausprobieren)</span></li>
      <li><a href="health">Healthcheck</a> <span class="muted">(JSON)</span></li>
    </ul>
  </section>"""

PIN_FORM = """<section class="card">
    <h2>Geräte und Export</h2>
    <p class="muted">Geräteliste und Export-Links sind mit einer PIN geschützt.</p>
    <form class="pin" method="post" action="login">
      <input type="password" name="pin" inputmode="numeric" autocomplete="current-password"
             placeholder="PIN" aria-label="PIN" required autofocus>
      <button type="submit">Anzeigen</button>
    </form>
    {message}
  </section>"""

PIN_MESSAGES = {
    "falsch": "PIN falsch.",
    "gesperrt": f"Zu viele Fehlversuche. Bitte in {auth.FAILURE_WINDOW // 60} Minuten erneut versuchen.",
}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> HTMLResponse:
    state = request.app.state
    settings = state.settings
    poller = state.poller
    tz = state.tz
    protected = bool(settings.api_token)
    pin_required = bool(settings.page_pin)
    unlocked = auth.page_unlocked(request)
    # With a PIN: details only after unlocking. Without: as before (devices only if the API is public)
    show_devices = unlocked if pin_required else not protected
    show_export = unlocked or not pin_required

    if poller.last_error:
        badge = '<span class="badge bad">Fehler</span>'
    elif poller.last_run is None:
        badge = '<span class="badge bad">Noch kein Abruf</span>'
    else:
        badge = '<span class="badge ok">OK</span>'
    error = (
        f'<div class="error">{escape(poller.last_error)}</div>' if poller.last_error else ""
    )

    devices_html = ""
    if show_devices:
        with state.session_factory() as session:
            counts = dict(
                session.execute(
                    select(Reading.device_id, func.count()).group_by(Reading.device_id)
                ).all()
            )
            devices = list(
                session.scalars(select(Device).where(Device.active.is_(True)).order_by(Device.code))
            )
        rows = "".join(
            "<tr>"
            f"<td><a href=\"api/export?devices={escape(d.code)}\"><b>{escape(d.code)}</b></a></td>"
            f"<td>{escape(d.name or '')}</td>"
            f"<td>{_online(d.online)}</td>"
            f"<td class=\"num\">{escape(_short(fmt_time(d.last_seen, tz)))}</td>"
            f"<td class=\"num\">{counts.get(d.id, 0)}</td>"
            "</tr>"
            for d in devices
        )
        devices_html = (
            '<section class="card"><h2>Geräte</h2><div class="table-wrap"><table>'
            "<tr><th>Code</th><th>Name</th><th>Status</th><th>Zuletzt gesehen</th>"
            "<th>Messungen</th></tr>"
            f"{rows or '<tr><td colspan=5 class=muted>Keine Geräte konfiguriert</td></tr>'}"
            "</table></div></section>"
        )

    token_hint = (
        '<p class="muted">Die API ist geschützt: Token als <code>?token=…</code> anhängen '
        "oder als Header <code>Authorization: Bearer …</code> senden.</p>"
        if protected and show_export and not unlocked
        else ""
    )
    pin_form = ""
    if pin_required and not unlocked:
        message = PIN_MESSAGES.get(request.query_params.get("pin", ""), "")
        pin_form = PIN_FORM.format(
            message=f'<div class="error">{escape(message)}</div>' if message else ""
        )
    logout = (
        ' · <form method="post" action="logout"><button class="link" type="submit">Sperren</button></form>'
        if unlocked
        else ""
    )

    html = PAGE.format(
        badge=badge,
        error=error,
        last_poll=escape(_short(fmt_time(poller.last_run, tz)) or "–"),
        interval=settings.poll_interval,
        retention=(
            f"Minutenwerte werden {settings.raw_retention_months} Monate aufbewahrt, ältere als Stundenwerte"
            if settings.raw_retention_months
            else "Minutenwerte werden unbegrenzt aufbewahrt"
        ),
        device_count=len(settings.devices),
        devices=devices_html,
        export=EXPORT.format(token_hint=token_hint) if show_export else "",
        pin_form=pin_form,
        logout=logout,
        now=escape(_short(fmt_time(utcnow(), tz))),
    )
    return HTMLResponse(html)


@router.post("/login", include_in_schema=False)
async def login(request: Request) -> RedirectResponse:
    settings = request.app.state.settings
    if not settings.page_pin:
        return RedirectResponse("./", status_code=303)
    if auth.login_locked(request):
        return RedirectResponse("./?pin=gesperrt", status_code=303)
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    pin = (form.get("pin") or [""])[0]
    if not auth.pin_correct(settings, pin):
        auth.record_failure(request)
        return RedirectResponse("./?pin=falsch", status_code=303)
    auth.clear_failures(request)
    response = RedirectResponse("./", status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME, auth.unlock_token(settings), max_age=auth.COOKIE_MAX_AGE,
        httponly=True, samesite="lax", secure=auth.is_https(request),
    )
    return response


@router.post("/logout", include_in_schema=False)
def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse("./", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


def _online(value: bool | None) -> str:
    if value is None:
        return '<span class="muted">–</span>'
    return '<span class="badge ok">online</span>' if value else '<span class="badge bad">offline</span>'


def _short(iso: str | None) -> str:
    """'2026-11-23T09:15:02+01:00' -> '23.11.2026 09:15:02'."""
    if not iso:
        return ""
    date_part, time_part = iso[:10], iso[11:19]
    y, m, d = date_part.split("-")
    return f"{d}.{m}.{y} {time_part}"

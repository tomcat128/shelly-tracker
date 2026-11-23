"""Small HTML start page: health status, devices and links to the export."""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

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

  <section class="card">
    <h2>Export</h2>
    {token_hint}
    <ul class="links">
      <li><a href="api/export">Letzte Werte</a> <span class="muted">(JSON)</span></li>
      <li><a href="api/export?from=24h">Letzte 24 Stunden</a> <span class="muted">(JSON)</span></li>
      <li><a href="api/export?from=24h&amp;format=csv-wide">Letzte 24 Stunden</a> <span class="muted">(CSV für Excel)</span></li>
      <li><a href="docs">API-Dokumentation</a> <span class="muted">(alle Parameter, zum Ausprobieren)</span></li>
      <li><a href="health">Healthcheck</a> <span class="muted">(JSON)</span></li>
    </ul>
  </section>

  <footer>Seite aktualisiert sich jede Minute · Stand {now}</footer>
</main>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> HTMLResponse:
    state = request.app.state
    settings = state.settings
    poller = state.poller
    tz = state.tz
    protected = bool(settings.api_token)

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
    if not protected:  # device details only when the API itself is public
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
        if protected
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
        token_hint=token_hint,
        now=escape(_short(fmt_time(utcnow(), tz))),
    )
    return HTMLResponse(html)


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

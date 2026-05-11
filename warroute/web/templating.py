"""Shared Jinja2 environment + render helper."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from warroute import __version__

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["app_version"] = __version__


def render(request: Request, template: str, **context: object) -> HTMLResponse:
    """Render a template with the request in scope (Jinja2Templates convention)."""
    return templates.TemplateResponse(request, template, context)

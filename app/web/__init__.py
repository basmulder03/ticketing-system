"""Server-rendered HTML (Jinja2 + HTMX): the backoffice, public site and
scanner pages. Routes are thin: they call the JSON API in-process and render
templates, so business rules live only in ``app.api``.
"""

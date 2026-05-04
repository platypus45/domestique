# templates/

Jinja2 templates rendered by FastAPI. Wired in `app.py`:

```python
templates = Jinja2Templates(directory="templates")
```

The whole folder ships into the PyInstaller bundle via
`("templates","templates")` in `domestique.spec` `datas=`.

## Templates

| File | Rendered by | Purpose |
|---|---|---|
| `dashboard.html` | `GET /` (post-setup) | Main app shell — Plan / Library / Routes / Track / Settings tabs, all of the dashboard charts and widgets. |
| `setup.html` | `GET /` (first run) | First-run wizard. Collects FTP, weight, ICU credentials, target hours/week. |
| `profile_picker.html` | `GET /profiles` | Multi-user profile selector. |
| `profile_setup.html` | `POST /profiles/new` | New-profile creation form. |

Templates load assets via FastAPI route URLs (`/static/...`), not
filesystem paths — moving the `static/` mount route is the only way
to break those references.

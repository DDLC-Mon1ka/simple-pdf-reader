# Contributing

## Before opening an issue

- Search existing issues first.
- Include the application version and Windows version.
- Provide exact reproduction steps and the expected behavior.
- Do not upload personal or confidential PDFs. Create a minimal sample instead.
- Do not include API keys, encrypted configuration files, or private logs.

## Development environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

VS Code launch and build tasks are included in `.vscode`.

## Pull requests

- Keep changes focused and explain the user-facing impact.
- Preserve offline behavior unless the change is explicitly an API feature.
- Test PDF opening, page navigation, search, annotation persistence, and both
  single-page and continuous layouts when relevant.
- Avoid adding large PDFs, videos, generated executables, or local environments.
- Update `CHANGELOG.md` for user-visible changes.

## Code style

- Python 3.12
- Four-space indentation
- Type annotations for new public helpers where practical
- User-facing interface text remains in Chinese unless a localization layer is
  added
- Never hardcode credentials or provider-specific secrets

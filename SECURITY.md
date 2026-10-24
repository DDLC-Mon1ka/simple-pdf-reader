# Security Policy

## Supported version

Security fixes are currently provided for the latest released version.

## Reporting a vulnerability

Please use GitHub's private security-advisory feature for this repository.
Do not open a public issue containing:

- API keys or access tokens
- Personal PDF files
- Private document text
- Full application-data archives

Include the application version, Windows version, a minimal reproduction, and
the potential impact.

## API keys

Never commit a real API key. The application stores keys with Windows DPAPI under
the current user's application-data directory. If a key is exposed, revoke it at
the provider immediately.

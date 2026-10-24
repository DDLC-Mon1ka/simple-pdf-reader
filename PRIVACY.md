# Privacy

## Local processing

PDF rendering, search, bookmarks, annotations, navigation history, reading
position, and local extractive summaries are processed on the local device.

The application does not collect telemetry, analytics, advertising identifiers,
or crash reports.

## Optional API features

AI summaries and document Q&A are optional. When one of these features is used:

- Text extracted from relevant PDF pages is sent to the API base URL configured
  by the user.
- The selected API provider may retain or process that text according to its own
  privacy policy.
- The user is responsible for checking whether the document may be sent to that
  provider.
- Image-only pages are not uploaded as images; they must first contain a text
  layer to be included.

## Credential storage

API keys are encrypted with Windows Data Protection API (DPAPI) for the current
Windows user account. The encrypted value is stored under the user's application
data directory, not in the repository.

The application never prints or embeds an API key in source code.

## Files stored locally

The application may save:

- Recent-file and reading-position state
- User bookmarks
- Sidecar annotation data
- Encrypted API configuration

Removing the application-data directory removes these local records.

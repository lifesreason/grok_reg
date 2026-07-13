# CPA Management API Push Design

## Goal

After a registration job successfully exports an `xai-*.json` CPA credential,
optionally upload that credential to a remote CLI Proxy API / CPA instance.
The registration result remains successful when the remote upload fails.

## Remote Contract

- Endpoint: `POST <base>/v0/management/auth-files`
- Authentication: `Authorization: Bearer <management key>`
- Body: `multipart/form-data` with one `file` part whose filename is the local
  CPA credential filename.
- Success: any HTTP 2xx response. The response body is logged only as a
  bounded diagnostic preview.

The configured base accepts a host root or an existing `/v0/management` path.
The client normalizes it to the endpoint above without duplicating the prefix.

## Configuration

Add three persisted settings to the existing Web console:

- `cpa_management_base`: CPA server host or management API base.
- `cpa_management_key`: CPA Management API key, masked in API responses.
- `cpa_auto_push_remote`: enable automatic push after a successful CPA export.

The feature is disabled by default. It is considered configured only when the
toggle is enabled and both base and key are non-empty.

## Data Flow

1. The existing CPA mint process writes the local `xai-*.json` file.
2. The export wrapper calls a dedicated upload helper when remote push is
   configured.
3. The helper validates the local file, normalizes the API URL, sends a
   multipart upload using the Management API key, and records the status.
4. Successful pushes are logged with the target host and credential filename.
   Keys and credential contents never appear in logs.
5. Failed pushes add `upload_error` to the export result and log the HTTP or
   network failure. They do not fail registration or discard the local file.

## Error Handling

- Missing configuration: skip without a network request.
- Missing local credential: return a local validation error.
- Non-2xx or network error: the upload helper returns a bounded error message;
  the already-successful CPA export stays `ok: true`, gains `upload_error`, and
  keeps the generated credential on disk.
- Existing CPA export failures retain their current handling and never attempt
  an upload.

## Tests

- URL normalization covers host roots and already-prefixed bases.
- A configured push sends `Authorization: Bearer ...` and a multipart `file`
  named after the local credential.
- A non-2xx response produces `upload_error` without changing a successful CPA
  export result into a registration failure.
- The Web configuration API masks the Management key.

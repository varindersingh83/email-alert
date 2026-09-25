# Activepieces SIT bootstrap record

This record captures the infrastructure startup only. It does not claim that Gmail is connected or that a workflow or Telegram message was tested.

## Deployment

- Date: 2026-09-25 (UTC log timestamps below)
- Railway project: `email-alert-sit`
- Environment: `SIT`
- Activepieces URL: <https://activepieces-sit.up.railway.app>
- Activepieces Community Edition: `0.91.0`
- Activepieces service: `4cf3ee26-9e1a-47eb-8a7a-5da006198890`
- Activepieces deployment: `87721a18-ee28-40c8-8d2f-501f45ab7763` (`SUCCESS`)
- New dependencies from the official template: `Postgres-Wi7c` deployment `fb625223-1a36-4237-91eb-18075692b356` (`SUCCESS`) and `Redis` deployment `7edbf1e7-8a5b-4dcd-bbed-05ff689d91da` (`SUCCESS`). These are separate from the existing Telegram sender's Postgres service.

## Startup evidence

Redacted to metadata; no email body, OAuth token, API key, database URL, or bot credential is recorded.

| UTC time | Evidence |
| --- | --- |
| `2026-09-25T00:24:52Z` | Activepieces `0.91.0` Community Edition started and listened on port 80. |
| `2026-09-25T00:24:53Z` | Railway health check on `/api/v1/pieces` returned HTTP 200. |
| `2026-09-25T00:24:56Z` | Activepieces worker connected to the API server and began polling. |
| Current Railway status at capture | Activepieces, its Postgres, Redis, and the existing sender services report `SUCCESS`/online; no staged changes. |

The first health-check request briefly returned service unavailable during application startup; the next check succeeded within the configured five-minute retry window.

## Not done yet

- The browser is on the Activepieces **Create your account** page. The client owner must create the first administrator account and keep recovery access. The page is public, so claim it promptly.
- No Gmail connection exists in this Activepieces workspace yet. Google OAuth has not been granted here.
- No `fizz` or `buzz` flow has been created or published.
- No test email was sent and no Activepieces flow run or Telegram delivery was verified.

The default Activepieces Gmail piece currently requests `gmail.readonly`, `gmail.modify`, `gmail.compose`, and `gmail.send` scopes (plus account email). The mailbox owner must review the Google consent screen and decide whether to grant these permissions before connecting the mailbox.

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

## Live setup check

Checked on 2026-09-25 against the Activepieces SIT workspace and the sender API using one harmless synthetic `fizz` email. No email body, OAuth token, API key, database URL, or bot credential is recorded here.

| Check | Result |
| --- | --- |
| Activepieces workspace | `https://activepieces-sit.up.railway.app` is available. Flow `Email content: fizz` exists in `Personal Project` and remains unpublished. |
| Gmail | `founders@asynccraft.com` is connected. The Gmail New Email trigger was tested against the harmless `fizz` sample and returned its message data. |
| OpenAI | The OpenAI connection is connected. Ask ChatGPT was tested and generated a summary from the sample email; the message text is intentionally omitted. |
| Sender key | The Activepieces project variable named `LOCAL_API_KEY` exists. Its value was not viewed or written to this repository. |
| Sender request | The flow's HTTP POST to `/send` returned HTTP `202` and job `5`. An authenticated status lookup returned HTTP `200`: job `5`, destination `default`, status `sent`, one attempt, Telegram message ID `9`; created at `2026-09-25T05:12:30Z`. |
| Flow state | The Gmail, OpenAI, and sender HTTP steps all show tested. The flow is still unpublished, so this test was manual; no live trigger is enabled. |

## Not done yet

- The `Email content: buzz` flow has not been created or tested.
- The `fizz` flow has not been published. New incoming email is not yet triggering the workflow automatically.
- No client-owned Activepieces project or client mailbox is configured; this is a SIT workspace under `Personal Project`.

The default Activepieces Gmail piece currently requests `gmail.readonly`, `gmail.modify`, `gmail.compose`, and `gmail.send` scopes (plus account email), even when the flow uses only New Email. Review the exact Google consent screen and mailbox policy before connecting a client mailbox.

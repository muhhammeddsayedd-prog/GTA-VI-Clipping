# n8n workflows

n8n owns schedules, the takedown Gmail intake and Telegram. It holds no business logic: every workflow
calls the control API or relays a message (`docs/13-n8n-workflows.md`).

The workflows are defined in `build_workflows.py` and created through the n8n public API. The JSON files
in `workflows/` are exports for review and backup (`<env>-WFxx-<name>.json`): credential ids, the Telegram
chat id, the notify-secret digest and instance ids are stripped. n8n remains the place the workflows run.

## Commands

```
python n8n/build_workflows.py apply   --env staging --shared-env <.env.shared> --env-file <.env.staging> --api-base <url>
python n8n/build_workflows.py publish --env staging --shared-env <.env.shared> --only WF01,WF02
python n8n/build_workflows.py unpublish --env staging --shared-env <.env.shared> --only WF01
python n8n/build_workflows.py status  --env staging --shared-env <.env.shared>
python n8n/build_workflows.py export  --env staging --shared-env <.env.shared>
```

- `--shared-env`: file with `N8N_BASE_URL`, `N8N_API_KEY`, `N8N_PROJECT_ID`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, optionally `N8N_GMAIL_CREDENTIAL_NAME`.
- `--env-file`: file with `CONTROL_API_TOKEN` and `N8N_NOTIFY_SECRET` of that environment.
- `apply` is idempotent. It creates the credentials `6stars Telegram` and `6stars API <env>` when they do
  not exist (it never changes an existing credential), creates or updates the nine workflows by their exact
  name, tags them `6stars` + `<env>`, and publishes WF09 (an error workflow must be published before others
  can point at it). Nothing else is activated: use `publish`. Updating a published workflow re-publishes it.
- `apply --save-success` keeps successful executions too, for testing. Run `apply` again without it after.
- A changed control API token or bot token: delete the credential in n8n and run `apply` again.

## Production set

```
python n8n/build_workflows.py apply --env production --shared-env <.env.shared> --env-file <.env.production> \
    --api-base <production control API base> [--gmail-credential "6stars Gmail"]
python n8n/build_workflows.py publish --env production --shared-env <.env.shared> --only WF07,WF01,WF02,...
python n8n/build_workflows.py export --env production --shared-env <.env.shared>
```

This creates `[PROD] WF01`–`[PROD] WF09`, the credential `6stars API production`, the notify path
`/webhook/6stars-notify-production` (put that URL into the production `N8N_NOTIFY_WEBHOOK_URL`), and
messages without the `[STG]` prefix. To target another n8n instance, point `N8N_BASE_URL`, `N8N_API_KEY`
and `N8N_PROJECT_ID` in the shared env file at it; the Gmail OAuth2 credential must already exist there.

## Things to know

- Every workflow starts with a `Config` node (`API_BASE`, `TELEGRAM_CHAT_ID`, `ENV_PREFIX`). Only that node
  and the credentials differ between environments.
- WF07 answers 200 immediately and compares the SHA-256 of the `X-6Stars-Secret` header with the digest in
  its `Config` node, so the secret itself is not stored in the workflow. No match: nothing is sent.
- WF08 (Telegram commands) uses the bot's single webhook. With one bot, only one environment may have it
  published; `[STG] WF08` stays unpublished.
- WF04 needs the Gmail label `6stars-processed` to exist in the takedown inbox before it is published.
- HTTP and Telegram nodes retry 3 tries, 5 s apart: 5 s is the most the n8n engine allows between tries.

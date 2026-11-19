"""Create, update, publish and export the 6 Stars Clips n8n workflows (WF01-WF09) through the n8n public API.

The workflow definitions live in this file, so the staging and the production set are the same graphs with
a different Config node and different credentials. Standard library only; run it with any Python 3.12.

    python n8n/build_workflows.py apply   --env staging --shared-env <.env.shared> --env-file <.env.staging> \
        --api-base https://api-staging.example.com
    python n8n/build_workflows.py publish --env staging --shared-env <.env.shared> --only WF09,WF07
    python n8n/build_workflows.py export  --env staging --shared-env <.env.shared>
    python n8n/build_workflows.py status  --env staging --shared-env <.env.shared>

`--shared-env` supplies N8N_BASE_URL, N8N_API_KEY, N8N_PROJECT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and
optionally N8N_GMAIL_CREDENTIAL_NAME. `--env-file` supplies CONTROL_API_TOKEN and N8N_NOTIFY_SECRET.

Safety: the script only ever writes to workflows whose exact name is one of its own (`[STG] WFxx ...` or
`[PROD] WFxx ...`) inside the target project, and only creates credentials (it never changes or reads an
existing one). Secrets go into n8n credentials; the notify secret is stored in the workflow only as a
SHA-256 digest. Nothing secret is printed or written to the export files.
"""

# ruff: noqa: E501  (n8n expressions are long one-line strings)
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

Json = dict[str, Any]

EXPORT_DIR = Path(__file__).resolve().parent / "workflows"
ENVS: dict[str, Json] = {
    "staging": {"prefix": "[STG]", "message_prefix": "[STG]", "tag": "staging"},
    # Production messages carry no prefix (docs/13: the prefix marks staging).
    "production": {"prefix": "[PROD]", "message_prefix": "", "tag": "production"},
}
TELEGRAM_CREDENTIAL = "6stars Telegram"
HTTP_TIMEOUT_MS = 30_000
MAX_TRIES = 3
# The plan asks for 10 s between tries; the n8n engine caps the wait at 5000 ms, so 5000 is what is real.
WAIT_BETWEEN_TRIES_MS = 5_000
PROCESSED_LABEL = "6stars-processed"

# slug -> (id, display name, file slug, description)
WORKFLOWS: dict[str, tuple[str, str, str]] = {
    "WF01": (
        "Discovery Tick",
        "discovery",
        "Every 10 minutes, asks the control API to run one discovery pass.",
    ),
    "WF02": (
        "Hourly Poster",
        "hourly-poster",
        "At minute 0 of every hour, triggers the hourly posting slot.",
    ),
    "WF03": (
        "Guardian Tick",
        "guardian",
        "Every 15 minutes, asks the control API to check claims and removals.",
    ),
    "WF04": (
        "Takedown Intake",
        "takedown-intake",
        "Polls the takedown inbox every 5 minutes, forwards each mail to the control API (which decides "
        f"whether it is a takedown) and labels it {PROCESSED_LABEL}.",
    ),
    "WF05": (
        "Stats Collector",
        "stats",
        "Every 30 minutes, asks the control API to collect account and post stats (fixed-age snapshots first).",
    ),
    "WF06": (
        "Daily Report",
        "daily-report",
        "At 00:00 UTC, fetches the daily report and sends it to Telegram.",
    ),
    "WF07": (
        "Notify Relay",
        "notify-relay",
        "Relays notifications from the worker (sixstars.notify) to Telegram. Answers 200 at once; a request "
        "whose x-6stars-secret header does not match is dropped silently.",
    ),
    "WF08": (
        "Telegram Commands",
        "telegram-commands",
        "Owner commands /stop /start /status /report /resume <account> from the Telegram chat, relayed to the control API. "
        "One bot has one webhook, so only one environment may have this workflow active.",
    ),
    "WF09": (
        "Error Alert",
        "error-alert",
        "Error workflow of every 6 Stars workflow: sends the failure to Telegram.",
    ),
}

CONFIG = "Config"
CFG = f"$('{CONFIG}').first().json"


# ---------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------
def read_env_file(path: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path:
        return values
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class N8n:
    """Minimal client for the n8n public API (`/api/v1`)."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/") + "/api/v1"
        self._key = api_key

    def call(self, method: str, path: str, body: Any = None) -> Any:
        request = urllib.request.Request(
            self._base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "X-N8N-API-KEY": self._key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:800]
            raise SystemExit(f"n8n API {method} {path} failed: HTTP {exc.code} {detail}") from exc
        return json.loads(text) if text else None

    def list_all(self, path: str) -> list[Json]:
        items: list[Json] = []
        cursor = ""
        while True:
            sep = "&" if "?" in path else "?"
            page = self.call("GET", f"{path}{sep}limit=100" + (f"&cursor={cursor}" if cursor else ""))
            items.extend(page["data"])
            cursor = page.get("nextCursor") or ""
            if not cursor:
                return items


def workflow_name(env: str, wf_id: str) -> str:
    return f"{ENVS[env]['prefix']} {wf_id} {WORKFLOWS[wf_id][0]}"


def find_own_workflows(api: N8n, env: str, project_id: str) -> dict[str, Json]:
    """Our workflows by WF id, matched on the exact name inside the target project."""
    found: dict[str, Json] = {}
    for wf_id in WORKFLOWS:
        name = workflow_name(env, wf_id)
        query = urllib.parse.urlencode({"name": name, "projectId": project_id})
        matches = [
            w for w in api.list_all(f"/workflows?{query}") if w["name"] == name and not w.get("isArchived")
        ]
        if len(matches) > 1:
            raise SystemExit(f"More than one workflow is named {name!r}; resolve that by hand first.")
        if matches:
            found[wf_id] = matches[0]
    return found


# ---------------------------------------------------------------------------------------------------------
# Node builders
# ---------------------------------------------------------------------------------------------------------
def stable_id(*parts: str) -> str:
    """An id that stays the same on every run, so re-applying does not churn the exports."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "6stars/" + "/".join(parts)))


def node(name: str, node_type: str, version: float, parameters: Json, **extra: Any) -> Json:
    return {
        "name": name,
        "type": f"n8n-nodes-base.{node_type}",
        "typeVersion": version,
        "parameters": parameters,
    } | extra


def retry() -> Json:
    return {"retryOnFail": True, "maxTries": MAX_TRIES, "waitBetweenTries": WAIT_BETWEEN_TRIES_MS}


def config_node(ctx: Json, *, with_secret_digest: bool = False) -> Json:
    values = {
        "API_BASE": ctx["api_base"],
        "TELEGRAM_CHAT_ID": ctx["chat_id"],
        "ENV_PREFIX": ENVS[ctx["env"]]["message_prefix"],
    }
    if with_secret_digest:
        values["NOTIFY_SECRET_SHA256"] = ctx["secret_sha256"]
    assignments = [
        {"id": stable_id("field", k), "name": k, "value": v, "type": "string"} for k, v in values.items()
    ]
    return node(
        CONFIG,
        "set",
        3.4,
        {
            "mode": "manual",
            "assignments": {"assignments": assignments},
            "includeOtherFields": False,
            "options": {},
        },
        notes="The only node that differs between staging and production (besides the credentials).",
    )


def schedule(name: str, interval: Json) -> Json:
    return node(name, "scheduleTrigger", 1.2, {"rule": {"interval": [interval]}})


def api_request(ctx: Json, name: str, method: str, path: str, body: str | None = None) -> Json:
    parameters: Json = {
        "method": method,
        "url": f"={{{{ {CFG}.API_BASE }}}}{path}",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "options": {"timeout": HTTP_TIMEOUT_MS},
    }
    if body is not None:
        parameters |= {"sendBody": True, "specifyBody": "json", "jsonBody": body}
    return node(
        name, "httpRequest", 4.2, parameters, credentials={"httpHeaderAuth": ctx["api_credential"]}, **retry()
    )


def telegram_send(ctx: Json, name: str, text: str, *, chat_id: str | None = None) -> Json:
    # Always HTML: without an explicit parse mode the Telegram node uses Markdown, which swallows "[STG]".
    fields: Json = {"appendAttribution": False, "disable_web_page_preview": True, "parse_mode": "HTML"}
    parameters = {
        "resource": "message",
        "operation": "sendMessage",
        "chatId": chat_id or f"={{{{ {CFG}.TELEGRAM_CHAT_ID }}}}",
        "text": text,
        "additionalFields": fields,
    }
    return node(
        name, "telegram", 1.2, parameters, credentials={"telegramApi": ctx["telegram_credential"]}, **retry()
    )


def string_condition(left: str, right: str) -> Json:
    return {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [
            {
                "id": stable_id("condition", left, right),
                "leftValue": left,
                "rightValue": right,
                "operator": {"type": "string", "operation": "equals"},
            }
        ],
        "combinator": "and",
    }


def chain(*names: str) -> Json:
    """Connections for a straight line of nodes."""
    return {
        a: {"main": [[{"node": b, "type": "main", "index": 0}]]}
        for a, b in zip(names, names[1:], strict=False)
    }


def tick(ctx: Json, trigger: Json, request_name: str, path: str) -> tuple[list[Json], Json]:
    nodes = [trigger, config_node(ctx), api_request(ctx, request_name, "POST", path)]
    return nodes, chain(*(n["name"] for n in nodes))


# ---------------------------------------------------------------------------------------------------------
# Workflow definitions
# ---------------------------------------------------------------------------------------------------------
NOTIFY_TEXT = (
    "={{ (() => {\n"
    "  const b = $('Notify webhook').first().json.body ?? {};\n"
    "  const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
    "  const emoji = ({ critical: '🔴', warn: '🟠', info: '🔵' })[b.severity] ?? '🔵';\n"
    f"  const head = [{CFG}.ENV_PREFIX, emoji, '<b>' + esc(b.title) + '</b>'].filter(Boolean).join(' ');\n"
    "  const body = esc(b.body).slice(0, 3500);\n"
    "  return body ? head + '\\n' + body : head;\n"
    "})() }}"
)
ERROR_TEXT = (
    "={{ (() => {\n"
    "  const e = $('Error Trigger').first().json;\n"
    "  const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
    "  const message = e.execution?.error?.message ?? e.trigger?.error?.message ?? 'no error message';\n"
    "  const failedNode = e.execution?.lastNodeExecuted ? ' (node: ' + e.execution.lastNodeExecuted + ')' : '';\n"
    f"  const head = [{CFG}.ENV_PREFIX, '🔴 n8n workflow failed:', esc(e.workflow?.name ?? 'unknown workflow')];\n"
    "  const url = e.execution?.url ?? 'no execution (the trigger failed)';\n"
    "  return head.filter(Boolean).join(' ') + ' — ' + esc(message.slice(0, 1500) + failedNode) + ' — ' + url;\n"
    "})() }}"
)
COMMAND = (
    "={{ ($('Telegram Trigger').first().json.message?.text ?? '').trim().split(/\\s+/)[0].split('@')[0] }}"
)
# The whole Telegram message as `{"text": ...}`: commands with arguments (/resume <account>) parse it in the API.
MESSAGE_BODY = "={{ JSON.stringify({ text: $('Telegram Trigger').first().json.message?.text ?? '' }) }}"
REPLY_TEXT = (
    "={{ $json.text ?? ($json.stopped === true ? '⛔ Emergency stop is ON: nothing will be posted.'"
    " : $json.stopped === false ? '✅ Emergency stop is OFF: posting resumed.' : JSON.stringify($json)) }}"
)


def build(wf_id: str, ctx: Json) -> tuple[list[Json], Json]:
    """Nodes and connections of one workflow."""
    if wf_id == "WF01":
        trigger = schedule("Every 10 minutes", {"field": "minutes", "minutesInterval": 10})
        return tick(ctx, trigger, "Run discovery", "/v1/discovery/run")
    if wf_id == "WF02":
        trigger = schedule(
            "Every hour at minute 0", {"field": "hours", "hoursInterval": 1, "triggerAtMinute": 0}
        )
        return tick(ctx, trigger, "Trigger hourly slot", "/v1/posting/hourly")
    if wf_id == "WF03":
        trigger = schedule("Every 15 minutes", {"field": "minutes", "minutesInterval": 15})
        return tick(ctx, trigger, "Run guardian check", "/v1/guardian/check")
    if wf_id == "WF05":
        trigger = schedule("Every 30 minutes", {"field": "minutes", "minutesInterval": 30})
        return tick(ctx, trigger, "Collect stats", "/v1/stats/collect")
    if wf_id == "WF06":
        nodes = [
            schedule(
                "Daily at 00:00 UTC",
                {"field": "days", "daysInterval": 1, "triggerAtHour": 0, "triggerAtMinute": 0},
            ),
            config_node(ctx),
            api_request(ctx, "Fetch daily report", "GET", "/v1/reports/daily?format=telegram"),
            telegram_send(ctx, "Send report", "={{ $json.text }}"),
        ]
        return nodes, chain(*(n["name"] for n in nodes))
    if wf_id == "WF04":
        gmail = {"gmailOAuth2": ctx["gmail_credential"]}
        message = "$('Gmail Trigger').item.json"
        mapped = {
            "from": f"={{{{ {message}.from?.text ?? {message}.headers?.from ?? '' }}}}",
            "subject": f"={{{{ {message}.subject ?? '' }}}}",
            "body_text": f"={{{{ {message}.text || ({message}.html ?? '').replace(/<[^>]+>/g, ' ') }}}}",
            "received_at": f"={{{{ {message}.date ? DateTime.fromISO({message}.date).toUTC().toISO() : $now.toUTC().toISO() }}}}",
            "message_id": f"={{{{ {message}.id }}}}",
        }
        nodes = [
            node(
                "Gmail Trigger",
                "gmailTrigger",
                1.2,
                {
                    "pollTimes": {"item": [{"mode": "everyX", "value": 5, "unit": "minutes"}]},
                    "simple": False,
                    "filters": {"labelIds": ["INBOX"]},
                    "options": {},
                },
                credentials=gmail,
            ),
            config_node(ctx),
            node(
                "Map message",
                "set",
                3.4,
                {
                    "mode": "manual",
                    "assignments": {
                        "assignments": [
                            {"id": stable_id("field", k), "name": k, "value": v, "type": "string"}
                            for k, v in mapped.items()
                        ]
                    },
                    "includeOtherFields": False,
                    "options": {},
                },
            ),
            api_request(
                ctx, "Post takedown", "POST", "/v1/guardian/takedown", "={{ JSON.stringify($json) }}"
            ),
            node(
                "Find processed label",
                "httpRequest",
                4.2,
                {
                    "method": "GET",
                    "url": "https://gmail.googleapis.com/gmail/v1/users/me/labels",
                    "authentication": "predefinedCredentialType",
                    "nodeCredentialType": "gmailOAuth2",
                    "options": {"timeout": HTTP_TIMEOUT_MS},
                },
                credentials=gmail,
                notes=f"The Gmail node needs the label id; this looks up the id of the label {PROCESSED_LABEL}. "
                "The label must exist in the inbox, otherwise the next node fails and WF09 alerts.",
                **retry(),
            ),
            node(
                "Add processed label",
                "gmail",
                2.1,
                {
                    "resource": "message",
                    "operation": "addLabels",
                    "messageId": "={{ $('Map message').item.json.message_id }}",
                    "labelIds": f"={{{{ [$json.labels.find((l) => l.name === '{PROCESSED_LABEL}').id] }}}}",
                },
                credentials=gmail,
                **retry(),
            ),
        ]
        return nodes, chain(*(n["name"] for n in nodes))
    if wf_id == "WF07":
        secret_header = "={{ ($('Notify webhook').first().json.headers['x-6stars-secret'] ?? '').toString().hash('sha256') }}"
        nodes = [
            node(
                "Notify webhook",
                "webhook",
                2,
                {
                    "httpMethod": "POST",
                    "path": ctx["notify_path"],
                    "responseMode": "onReceived",
                    "options": {},
                },
            ),
            config_node(ctx, with_secret_digest=True),
            node(
                "Secret matches?",
                "if",
                2.2,
                {
                    "conditions": string_condition(secret_header, f"={{{{ {CFG}.NOTIFY_SECRET_SHA256 }}}}"),
                    "options": {},
                },
                notes="Compares SHA-256 digests so the secret itself is never stored in the workflow. "
                "No match: the false output is left unconnected on purpose (drop silently, the caller still got 200).",
            ),
            telegram_send(ctx, "Send notification", NOTIFY_TEXT),
        ]
        return nodes, chain(*(n["name"] for n in nodes))
    if wf_id == "WF09":
        nodes = [
            node("Error Trigger", "errorTrigger", 1, {}),
            config_node(ctx),
            telegram_send(ctx, "Send alert", ERROR_TEXT),
        ]
        return nodes, chain(*(n["name"] for n in nodes))
    if wf_id == "WF08":
        commands = {
            "/stop": api_request(
                ctx, "Stop posting", "POST", "/v1/control/stop", '{ "reason": "manual /stop" }'
            ),
            "/start": api_request(ctx, "Start posting", "POST", "/v1/control/start"),
            "/status": api_request(ctx, "Get status", "GET", "/v1/status?format=telegram"),
            "/report": api_request(ctx, "Get daily report", "GET", "/v1/reports/daily?format=telegram"),
            "/resume": api_request(
                ctx, "Resume hourly lane", "POST", "/v1/control/lane/resume", MESSAGE_BODY
            ),
        }
        rules = [
            {"conditions": string_condition(COMMAND, command), "renameOutput": True, "outputKey": command}
            for command in commands
        ]
        chat_id = "={{ String($('Telegram Trigger').first().json.message?.chat?.id ?? '') }}"
        nodes = [
            node(
                "Telegram Trigger",
                "telegramTrigger",
                1.2,
                {"updates": ["message"], "additionalFields": {}},
                credentials={"telegramApi": ctx["telegram_credential"]},
            ),
            config_node(ctx),
            node(
                "From the owner chat?",
                "if",
                2.2,
                {
                    "conditions": string_condition(chat_id, f"={{{{ {CFG}.TELEGRAM_CHAT_ID }}}}"),
                    "options": {},
                },
                notes="Messages from any other chat are ignored: the false output is unconnected on purpose.",
            ),
            node(
                "Which command?",
                "switch",
                3.2,
                {
                    "rules": {"values": rules},
                    "options": {"fallbackOutput": "extra", "renameFallbackOutput": "Other"},
                },
            ),
            *commands.values(),
            node("Ignore other messages", "noOp", 1, {}),
            telegram_send(ctx, "Reply", REPLY_TEXT, chat_id=chat_id),
        ]
        connections = chain("Telegram Trigger", CONFIG, "From the owner chat?", "Which command?")
        outputs = [*(n["name"] for n in commands.values()), "Ignore other messages"]
        connections["Which command?"] = {"main": [[{"node": n, "type": "main", "index": 0}] for n in outputs]}
        for request in commands.values():
            connections |= chain(request["name"], "Reply")
        return nodes, connections
    raise SystemExit(f"Unknown workflow {wf_id}")


def layout(nodes: list[Json], connections: Json) -> None:
    """Left-to-right positions; nodes fed by the same output fan out vertically."""
    position: dict[str, list[int]] = {nodes[0]["name"]: [0, 0]}
    queue = [nodes[0]["name"]]
    while queue:
        name = queue.pop(0)
        x, y = position[name]
        targets = [t["node"] for output in connections.get(name, {}).get("main", []) for t in output]
        for row, target in enumerate(targets):
            if target not in position:
                position[target] = [
                    x + 220,
                    y + (row - (len(targets) - 1) / 2) * 180 if len(targets) > 1 else y,
                ]
                queue.append(target)
    for n in nodes:
        n["position"] = [int(v) for v in position.get(n["name"], [0, 300])]


def workflow_body(
    wf_id: str, env: str, ctx: Json, existing: Json | None, error_workflow_id: str | None
) -> Json:
    nodes, connections = build(wf_id, ctx)
    layout(nodes, connections)
    # Keep node ids and webhook ids stable across updates (a Telegram webhook is registered under them).
    old = {n["name"]: n for n in (existing or {}).get("nodes", [])}
    for n in nodes:
        previous = old.get(n["name"], {})
        n["id"] = previous.get("id") or str(uuid.uuid4())
        if n["type"].endswith((".webhook", ".telegramTrigger")):
            n["webhookId"] = previous.get("webhookId") or str(uuid.uuid4())
    settings: Json = {
        "executionOrder": "v1",
        "timezone": "UTC",
        "saveDataErrorExecution": "all",
        "saveDataSuccessExecution": "all" if ctx["save_success"] else "none",
        "saveManualExecutions": True,
        "saveExecutionProgress": False,
    }
    if error_workflow_id and wf_id != "WF09":
        settings["errorWorkflow"] = error_workflow_id
    return {
        "name": workflow_name(env, wf_id),
        "description": WORKFLOWS[wf_id][2],
        "nodes": nodes,
        "connections": connections,
        "settings": settings,
    }


# ---------------------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------------------
def ensure_credential(
    api: N8n, known: list[Json], name: str, cred_type: str, data: Json, project_id: str
) -> Json:
    matches = [c for c in known if c["name"] == name and c["type"] == cred_type]
    if len(matches) > 1:
        raise SystemExit(
            f"More than one {cred_type} credential is named {name!r}; resolve that by hand first."
        )
    if matches:
        print(f"credential exists   {name} ({matches[0]['id']}) - left unchanged")
        return {"id": matches[0]["id"], "name": name}
    created = api.call(
        "POST", "/credentials", {"name": name, "type": cred_type, "data": data, "projectId": project_id}
    )
    print(f"credential created  {name} ({created['id']})")
    return {"id": created["id"], "name": name}


def ensure_tags(api: N8n, names: list[str]) -> list[Json]:
    existing = {t["name"]: t["id"] for t in api.list_all("/tags")}
    for name in names:
        if name not in existing:
            existing[name] = api.call("POST", "/tags", {"name": name})["id"]
            print(f"tag created         {name}")
    return [{"id": existing[name]} for name in names]


def is_active(workflow: Json) -> bool:
    return bool(workflow.get("activeVersionId") or workflow.get("active"))


def cmd_apply(api: N8n, args: argparse.Namespace, shared: dict[str, str], env_values: dict[str, str]) -> None:
    env = args.env
    project_id = shared["N8N_PROJECT_ID"]
    missing = [k for k in ("CONTROL_API_TOKEN", "N8N_NOTIFY_SECRET") if not env_values.get(k)]
    missing += [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not shared.get(k)]
    if missing or not args.api_base:
        raise SystemExit(f"apply needs --api-base and these values: {', '.join(missing) or '(all present)'}")

    credentials = api.list_all("/credentials")
    gmail_name = args.gmail_credential or shared.get("N8N_GMAIL_CREDENTIAL_NAME") or "6stars Gmail"
    gmail = [c for c in credentials if c["name"] == gmail_name and c["type"] == "gmailOAuth2"]
    if len(gmail) != 1:
        raise SystemExit(
            f"Expected exactly one gmailOAuth2 credential named {gmail_name!r}, found {len(gmail)}."
        )
    ctx: Json = {
        "env": env,
        "api_base": args.api_base.rstrip("/"),
        "chat_id": shared["TELEGRAM_CHAT_ID"],
        "secret_sha256": hashlib.sha256(env_values["N8N_NOTIFY_SECRET"].encode()).hexdigest(),
        "notify_path": f"6stars-notify-{env}",
        "save_success": args.save_success,
        "gmail_credential": {"id": gmail[0]["id"], "name": gmail_name},
        "telegram_credential": ensure_credential(
            api,
            credentials,
            TELEGRAM_CREDENTIAL,
            "telegramApi",
            {"accessToken": shared["TELEGRAM_BOT_TOKEN"]},
            project_id,
        ),
        "api_credential": ensure_credential(
            api,
            credentials,
            f"6stars API {env}",
            "httpHeaderAuth",
            {"name": "Authorization", "value": f"Bearer {env_values['CONTROL_API_TOKEN']}"},
            project_id,
        ),
    }
    tags = ensure_tags(api, ["6stars", ENVS[env]["tag"]])
    own = find_own_workflows(api, env, project_id)
    wanted = [w for w in ["WF09", *[w for w in WORKFLOWS if w != "WF09"]] if not args.only or w in args.only]
    error_workflow_id = own.get("WF09", {}).get("id")
    for wf_id in wanted:
        existing = own.get(wf_id)
        body = workflow_body(wf_id, env, ctx, existing, error_workflow_id)
        if existing:
            saved = api.call("PUT", f"/workflows/{existing['id']}", body)
            print(f"workflow updated    {body['name']} ({saved['id']})")
        else:
            saved = api.call("POST", "/workflows", body | {"projectId": project_id})
            print(f"workflow created    {body['name']} ({saved['id']})")
        api.call("PUT", f"/workflows/{saved['id']}/tags", tags)
        if wf_id == "WF09":
            # An error workflow must be published before other workflows may point at it.
            if not is_active(saved):
                api.call("POST", f"/workflows/{saved['id']}/publish")
                print(f"workflow published  {body['name']}")
            error_workflow_id = saved["id"]
    print(f"notify webhook URL  {shared['N8N_BASE_URL'].rstrip('/')}/webhook/{ctx['notify_path']}")


def cmd_publish(api: N8n, args: argparse.Namespace, shared: dict[str, str], unpublish: bool = False) -> None:
    own = find_own_workflows(api, args.env, shared["N8N_PROJECT_ID"])
    if not args.only:
        raise SystemExit("Name the workflows with --only, for example --only WF09,WF07")
    for wf_id in args.only:
        if wf_id not in own:
            raise SystemExit(f"{workflow_name(args.env, wf_id)} does not exist; run apply first.")
        api.call("POST", f"/workflows/{own[wf_id]['id']}/{'unpublish' if unpublish else 'publish'}")
        print(f"{'unpublished' if unpublish else 'published'}  {own[wf_id]['name']} ({own[wf_id]['id']})")


def cmd_status(api: N8n, args: argparse.Namespace, shared: dict[str, str]) -> None:
    own = find_own_workflows(api, args.env, shared["N8N_PROJECT_ID"])
    for wf_id in WORKFLOWS:
        workflow = own.get(wf_id)
        state = "missing" if workflow is None else ("ACTIVE" if is_active(workflow) else "inactive")
        print(
            f"{wf_id}  {state:8}  {workflow['id'] if workflow else '-':16}  {workflow_name(args.env, wf_id)}"
        )


def strip_for_export(workflow: Json, error_workflow_name: str) -> Json:
    """The workflow as committed to the repo: no ids of this instance, no chat id, no secret digest."""
    nodes = []
    for original in workflow["nodes"]:
        n = json.loads(json.dumps(original))
        n.pop("webhookId", None)
        if n["type"].endswith(".telegramTrigger"):
            n.pop("id", None)
        for credential in (n.get("credentials") or {}).values():
            credential.pop("id", None)
        if n["name"] == CONFIG:
            for assignment in n["parameters"]["assignments"]["assignments"]:
                if assignment["name"] == "TELEGRAM_CHAT_ID":
                    assignment["value"] = "<TELEGRAM_CHAT_ID>"
                if assignment["name"] == "NOTIFY_SECRET_SHA256":
                    assignment["value"] = "<sha256 hex digest of N8N_NOTIFY_SECRET>"
        nodes.append(n)
    settings = dict(workflow.get("settings") or {})
    if settings.get("errorWorkflow"):
        settings["errorWorkflow"] = f"<id of {error_workflow_name}>"
    return {
        "name": workflow["name"],
        "description": workflow.get("description"),
        "active": is_active(workflow),
        "nodes": nodes,
        "connections": workflow["connections"],
        "settings": settings,
        "tags": sorted(t["name"] for t in workflow.get("tags") or []),
    }


def cmd_export(api: N8n, args: argparse.Namespace, shared: dict[str, str]) -> None:
    own = find_own_workflows(api, args.env, shared["N8N_PROJECT_ID"])
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for wf_id, summary in sorted(own.items()):
        workflow = api.call("GET", f"/workflows/{summary['id']}")
        path = EXPORT_DIR / f"{args.env}-{wf_id}-{WORKFLOWS[wf_id][1]}.json"
        exported = strip_for_export(workflow, workflow_name(args.env, "WF09"))
        path.write_text(json.dumps(exported, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"exported  {path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["apply", "publish", "unpublish", "export", "status"])
    parser.add_argument("--env", required=True, choices=sorted(ENVS))
    parser.add_argument(
        "--shared-env", required=True, help="Env file with the n8n and Telegram access values."
    )
    parser.add_argument(
        "--env-file", help="Env file of the environment (CONTROL_API_TOKEN, N8N_NOTIFY_SECRET)."
    )
    parser.add_argument("--api-base", help="Control API base URL, without a trailing slash.")
    parser.add_argument(
        "--gmail-credential", help="Name of the Gmail OAuth2 credential of the takedown inbox."
    )
    parser.add_argument(
        "--only", type=lambda v: [p.strip().upper() for p in v.split(",") if p.strip()], default=[]
    )
    parser.add_argument(
        "--save-success",
        action="store_true",
        help="Also keep successful executions (for testing). Run apply again without it afterwards.",
    )
    args = parser.parse_args()
    unknown = [w for w in args.only if w not in WORKFLOWS]
    if unknown:
        raise SystemExit(f"Unknown workflow ids: {', '.join(unknown)}")
    shared = read_env_file(args.shared_env)
    api = N8n(shared["N8N_BASE_URL"], shared["N8N_API_KEY"])
    if args.command == "apply":
        cmd_apply(api, args, shared, read_env_file(args.env_file))
    elif args.command in ("publish", "unpublish"):
        cmd_publish(api, args, shared, unpublish=args.command == "unpublish")
    elif args.command == "export":
        cmd_export(api, args, shared)
    else:
        cmd_status(api, args, shared)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    main()

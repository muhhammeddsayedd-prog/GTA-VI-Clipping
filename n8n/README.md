# n8n workflows

Build the workflows described in `docs/13-n8n-workflows.md` in your n8n instance, then export each one (n8n → workflow → ⋯ → Download) into `n8n/workflows/` as:

```
n8n/workflows/staging-WF01-discovery.json
n8n/workflows/production-WF01-discovery.json
...
```

Remove credential IDs before committing if your instance includes them. These files are backups and documentation; n8n remains the place the workflows run.

Milestone M12 covers building them. If you use the n8n MCP server with Claude Code, it can create them directly from `docs/13`.

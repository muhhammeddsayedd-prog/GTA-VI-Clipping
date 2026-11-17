# M17 — Admin console

**Goal:** a private web console where the single operator can see every process in the system and use the two existing controls (emergency stop/start, settings). Decision D109; design and API contract in `docs/26-admin-console.md`.

**Depends on:** M11 (control API). It must not delay or destabilise M15/M16. Nothing from M17 reaches `main` before M16 is done.

## Scope

1. **Read API.** Read-only `/v1/admin/*` routes, accepted with a new `DASHBOARD_API_TOKEN`. The n8n token keeps working everywhere; the dashboard token is refused on the trigger routes. Migration `0018_admin_console.sql` adds the `admin.*` settings.
2. **Web app.** `web/`: Next.js 16 on Vercel, with:
   - a backend-for-frontend proxy;
   - Supabase Auth for one operator;
   - every screen in the approved mockups: Overview, Pipeline, Live, VOD, Spikes, Clips, Queue, Posts, Discovery, Creators, Accounts, Guardian, Health, Alerts, Jobs, Events, Spend, Reports and Settings, plus modals, drawers, the command palette and mobile layouts.
3. **CI.** A `web` job runs lint, types, unit tests and the build, and checks that no secret name ends up in a client bundle.
4. **Deploy.** One Vercel project per environment (staging first). Sign-ups are off in that environment's Supabase project, and the operator user is created by hand.

## Definition of done

- **Python checks.** `ruff`, `mypy --strict` and `pytest` pass, including the new admin route tests.
- **Web checks.** `npm run lint`, `npm run typecheck`, `npm test` and `npm run build` pass in `web/`.
- **Staging console.**
  - The operator signs in and any other account is refused.
  - Every page loads real data with no "SAMPLE DATA" band.
  - A posted clip can be traced from creator to post.
  - Stop and resume round-trip, and the Telegram alerts arrive.
  - A harmless setting change shows up as a `setting.changed` event.
- **Repo hygiene.** No token, password or service-role key is in the repo or in a client bundle.

# 6 Stars Clips — admin console (`web/`)

Private console for the single operator. It shows every part of the pipeline: overview, pipeline flow, live sessions, VOD queue, spikes, clips (player + quality gate), posting queue, posts, discovery, creators, accounts, guardian, health, alerts, jobs, events, spend, reports and settings. The only controls are the ones the control API already has: emergency stop/start and the settings editor. Design and API contract: [`docs/26-admin-console.md`](../docs/26-admin-console.md). Decision: D109.

## Stack

Next.js 16 (App Router, `proxy.ts`), React 19, TypeScript, Tailwind 4 plus the console stylesheet (`app/console.css`, taken from the approved mockups), TanStack Query (polling), motion, and [React Bits](https://reactbits.dev) components in `components/reactbits/` (CountUp, AnimatedContent, FadeContent, SpotlightCard, ShinyText, GradientText, Aurora, BlurText). Supabase Auth via `@supabase/ssr`.

## How it is wired

- The browser only talks to this app. `app/api/backend/[...path]` is a backend-for-frontend:
  - it checks the session and the operator email;
  - it allows a fixed list of routes (`lib/guards.ts`): admin reads, `status`, `reports/daily`, and the three control writes;
  - it adds `DASHBOARD_API_TOKEN` on the server.
- Trigger routes (discovery/run, posting/hourly, …) are never reachable from the console.
- `proxy.ts` refreshes the Supabase session and sends visitors without one to `/login`. The real check runs in `app/(console)/layout.tsx` and in the API proxy (`lib/server/auth.ts`).
- Data is polled: status every 10 s, lists every 30 s, paused while the tab is hidden.

## Run locally

```bash
cd web
npm install
cp .env.example .env.local      # set ADMIN_EMAIL, DEV_ADMIN_PASSWORD, SESSION_SECRET; leave Supabase and API empty
npm run dev                      # http://localhost:3000
```

- **Dev login.** With no Supabase settings, sign-in uses the dev login: the `ADMIN_EMAIL` plus `DEV_ADMIN_PASSWORD`.
- **Sample data.** With no `CONTROL_API_URL`, the console shows generated sample data from `lib/mock/`, behind a purple "SAMPLE DATA" band.
- **Vercel.** Neither the dev login nor sample data is ever used on Vercel.
- **Real API.** To point a local console at a real control API, set `CONTROL_API_URL` and `DASHBOARD_API_TOKEN`.

Checks (the same ones CI runs):

```bash
npm run lint && npm run typecheck && npm test && npm run build
```

## Deploy (one Vercel project per environment)

1. **Create the projects.** Make one Vercel project per environment (`6stars-console-staging`, `6stars-console-production`) from this repo, with **Root Directory `web`**. Set the production branch to `develop` for staging and `main` for production.
2. **Set up Supabase Auth.** In the environment's own Supabase project, under Authentication:
   - turn **off** "Allow new users to sign up";
   - add the operator user (email + password) by hand under Users;
   - set the Site URL to the console URL.
3. **Server.** Add `DASHBOARD_API_TOKEN` (a long random string, 32+ characters) to the server `.env` of that environment and redeploy the API.
4. **Vercel environment variables.** Set these in the Vercel project: `APP_ENV`, `ADMIN_EMAIL`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `CONTROL_API_URL`, `DASHBOARD_API_TOKEN`.
5. **Check it.** Deploy, sign in, and confirm the header shows the right environment chip and no "SAMPLE DATA" band.

Never put the password, the dashboard token or a service-role key in the repo: it is public.

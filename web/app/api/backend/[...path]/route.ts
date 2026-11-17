import { NextResponse, type NextRequest } from "next/server";
import { randomUUID } from "node:crypto";

import { getOperator } from "@/lib/server/auth";
import { serverEnv, mockApiEnabled } from "@/lib/server/env";
import { mockHandle } from "@/lib/mock";
import { allowedPath, sameOrigin } from "@/lib/guards";

/**
 * Backend-for-frontend: the only way the browser reaches the control API. Checks the operator session,
 * allows a fixed set of routes, and adds the dashboard token on the server. The token never reaches the browser.
 */

const TIMEOUT_MS = 15_000;

type Ctx = { params: Promise<{ path: string[] }> };

function fail(status: number, code: string, message: string) {
  return NextResponse.json({ error: { code, message } }, { status, headers: { "Cache-Control": "no-store" } });
}

async function forward(req: NextRequest, method: "GET" | "POST", path: string, body?: unknown) {
  const query = req.nextUrl.searchParams;
  if (mockApiEnabled()) {
    const res = await mockHandle(method, path, query, body);
    return NextResponse.json(res.body, { status: res.status, headers: { "Cache-Control": "no-store", "X-Mock-Data": "1" } });
  }
  const url = `${serverEnv.apiUrl}/v1/${path}${query.size ? `?${query.toString()}` : ""}`;
  try {
    const upstream = await fetch(url, {
      method,
      headers: {
        Authorization: `Bearer ${serverEnv.apiToken}`,
        "X-Request-ID": randomUUID(),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    const text = await upstream.text();
    return new NextResponse(text, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return fail(502, "upstream_unreachable", "The pipeline server did not answer.");
  }
}

export async function GET(req: NextRequest, { params }: Ctx) {
  if (!(await getOperator())) return fail(401, "unauthorized", "sign in first");
  const path = (await params).path.join("/");
  if (!allowedPath("GET", path)) return fail(404, "not_found", "unknown route");
  return forward(req, "GET", path);
}

export async function POST(req: NextRequest, { params }: Ctx) {
  if (!(await getOperator())) return fail(401, "unauthorized", "sign in first");
  if (!sameOrigin(req.headers.get("origin"), req.headers.get("host"))) return fail(403, "forbidden", "cross-site request refused");
  const path = (await params).path.join("/");
  if (!allowedPath("POST", path)) return fail(404, "not_found", "unknown route");
  let body: unknown = undefined;
  const raw = await req.text();
  if (raw) {
    if (raw.length > 64_000) return fail(413, "too_large", "request body too large");
    try {
      body = JSON.parse(raw);
    } catch {
      return fail(400, "bad_json", "body must be JSON");
    }
  }
  return forward(req, "POST", path, body);
}

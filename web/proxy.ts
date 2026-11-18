import { NextResponse, type NextRequest } from "next/server";
import { createServerClient } from "@supabase/ssr";

import { authMode, serverEnv } from "@/lib/server/env";
import { DEV_COOKIE } from "@/lib/server/constants";

const PUBLIC = ["/login", "/denied"];

/**
 * Optimistic gate: refreshes the Supabase session cookie and sends visitors without a session to /login.
 * The real check (session verified with the auth server + operator email) runs in the console layout and the API proxy.
 */
export async function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const isPublic = PUBLIC.some((p) => pathname === p || pathname.startsWith(`${p}/`));
  const isApi = pathname.startsWith("/api/");
  let response = NextResponse.next({ request });
  let hasSession = false;

  const mode = authMode();
  if (mode === "supabase") {
    const supabase = createServerClient(serverEnv.supabaseUrl!, serverEnv.supabaseKey!, {
      cookies: {
        getAll: () => request.cookies.getAll(),
        setAll: (list) => {
          for (const { name, value } of list) request.cookies.set(name, value);
          response = NextResponse.next({ request });
          for (const { name, value, options } of list) response.cookies.set(name, value, options);
        },
      },
    });
    const { data } = await supabase.auth.getClaims();
    hasSession = Boolean(data?.claims);
  } else if (mode === "dev") {
    hasSession = Boolean(request.cookies.get(DEV_COOKIE)?.value);
  }

  if (!hasSession && !isPublic) {
    if (isApi) return NextResponse.json({ error: { code: "unauthorized", message: "sign in first" } }, { status: 401 });
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname + search)}`;
    return NextResponse.redirect(url);
  }

  response.headers.set("X-Robots-Tag", "noindex, nofollow");
  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.png|apple-icon.png|brand/|robots.txt).*)"],
};

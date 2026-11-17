/** Console path for an `events.entity_type` / `entity_id` pair, or null when there is no page for it. */
export function entityHref(type: string | null, id: string | null): string | null {
  if (!type || !id) return null;
  const enc = encodeURIComponent(id);
  switch (type) {
    case "clip": return `/clips/${enc}`;
    case "spike": return `/spikes/${enc}`;
    case "post": return `/posts/${enc}`;
    case "video": return `/videos/${enc}`;
    case "live_session": return `/live/${enc}`;
    case "creator": return `/creators/${enc}`;
    case "account": return `/accounts/${enc}`;
    case "takedown": return `/guardian/takedowns/${enc}`;
    case "job": return `/jobs?job=${enc}`;
    case "incident": return "/health";
    case "setting": return `/settings?key=${enc}`;
    default: return null;
  }
}

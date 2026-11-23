import { Activity, Bell, Bolt, Compass, DollarSign, FileText, Film, GitBranch, Image, Layers, LayoutGrid, List, Palette, Radio, Send, Server, Shield, SlidersHorizontal, TrendingUp, Users, UserSquare2, Video } from "lucide-react";
import type { LucideIcon } from "lucide-react";

export interface NavItem { href: string; label: string; icon: LucideIcon; hint?: string }
export interface NavGroup { key: string; label: string; icon: LucideIcon; href?: string; items?: NavItem[] }

export const NAV: NavGroup[] = [
  { key: "overview", label: "Overview", icon: LayoutGrid, href: "/" },
  { key: "pipeline", label: "Pipeline", icon: GitBranch, href: "/pipeline" },
  {
    key: "content", label: "Content", icon: Film, items: [
      { href: "/live", label: "Live sessions", icon: Radio, hint: "Streams being recorded" },
      { href: "/videos", label: "VOD queue", icon: Video, hint: "Finished streams and uploads" },
      { href: "/spikes", label: "Spikes", icon: Bolt, hint: "Chat spikes and Claude verdicts" },
      { href: "/clips", label: "Clips", icon: Film, hint: "Rendered clips and the quality gate" },
      { href: "/queue", label: "Posting queue", icon: Layers, hint: "What posts next and why" },
      { href: "/posts", label: "Posts", icon: Send, hint: "Every post on every account" },
    ],
  },
  {
    key: "sources", label: "Sources", icon: Compass, items: [
      { href: "/discovery", label: "Discovery", icon: Compass, hint: "Trend scoring and sources" },
      { href: "/creators", label: "Creators", icon: Users, hint: "Permission tiers" },
      { href: "/accounts", label: "Accounts", icon: UserSquare2, hint: "Caps, pauses, followers" },
    ],
  },
  { key: "guardian", label: "Guardian", icon: Shield, href: "/guardian" },
  {
    key: "ops", label: "Operations", icon: Server, items: [
      { href: "/health", label: "Health", icon: Activity, hint: "Services, triggers, incidents" },
      { href: "/alerts", label: "Alerts", icon: Bell, hint: "Everything sent to Telegram" },
      { href: "/jobs", label: "Jobs", icon: Layers, hint: "The Postgres job queue" },
      { href: "/events", label: "Events", icon: List, hint: "Full audit log" },
    ],
  },
  {
    key: "insights", label: "Insights", icon: TrendingUp, items: [
      { href: "/spend", label: "Spend", icon: DollarSign, hint: "API spend and quotas" },
      { href: "/reports", label: "Reports", icon: FileText, hint: "Daily report" },
      { href: "/styles", label: "Design styles", icon: Palette, hint: "Which look works on each account" },
      { href: "/covers", label: "Covers", icon: Image, hint: "Thumbnails as each grid shows them" },
    ],
  },
  { key: "settings", label: "Settings", icon: SlidersHorizontal, href: "/settings" },
];

export function activeOf(pathname: string): { group: NavGroup | undefined; item: NavItem | undefined } {
  const match = (href: string) => (href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`));
  for (const g of NAV) {
    if (g.href && match(g.href)) return { group: g, item: undefined };
    const it = g.items?.find((i) => match(i.href));
    if (it) return { group: g, item: it };
  }
  return { group: undefined, item: undefined };
}

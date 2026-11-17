import Link from "next/link";
import { LayoutGrid } from "lucide-react";

import GradientText from "@/components/reactbits/GradientText";

export default function NotFound() {
  return (
    <div className="app" style={{ minHeight: "calc(100vh - 56px)", display: "grid", placeItems: "center" }}>
      <div className="empty">
        <div>
          <div style={{ fontSize: 84, fontWeight: 700, letterSpacing: "-.04em", lineHeight: 1 }}>
            <GradientText colors={["#8C80FF", "#4B3BDF", "#9F94FF"]} animationSpeed={6}>404</GradientText>
          </div>
          <h2 style={{ marginTop: 12 }}>Nothing lives at this address</h2>
          <p>The clip, post or page may have been pruned (events and candidates are kept 30 days) or the link is wrong.</p>
          <div className="row" style={{ justifyContent: "center" }}>
            <Link href="/" className="btn p"><LayoutGrid size={15} />Go to overview</Link>
          </div>
        </div>
      </div>
    </div>
  );
}

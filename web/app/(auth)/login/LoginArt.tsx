"use client";

import { motion } from "motion/react";
import { ArrowUpRight } from "lucide-react";

import Aurora from "@/components/reactbits/Aurora";
import BlurText from "@/components/reactbits/BlurText";
import CountUp from "@/components/reactbits/CountUp";
import { Bars } from "@/components/kit/charts";
import { Thumb } from "@/components/kit/data";
import { Logo } from "@/components/shell/Logo";

const float = (delay: number) => ({
  initial: { opacity: 0, y: 24 },
  animate: { opacity: 1, y: [0, -8, 0] },
  transition: { opacity: { duration: 0.6, delay }, y: { duration: 6, repeat: Infinity, ease: "easeInOut" as const, delay } },
});

export function LoginArt() {
  return (
    <div className="art" aria-hidden>
      <div style={{ position: "absolute", inset: 0, opacity: 0.55, mixBlendMode: "screen", pointerEvents: "none" }}>
        <Aurora colorStops={["#9F94FF", "#5A4AF4", "#2B1E9E"]} amplitude={1.1} blend={0.6} speed={0.6} />
      </div>
      <div style={{ position: "relative", zIndex: 2 }}>
        <div className="row" style={{ gap: 12 }}>
          <Logo />
          <div><b style={{ fontSize: 17 }}>6 STARS</b><div style={{ fontSize: 11, color: "#D7D2FF" }}>Clips control console</div></div>
        </div>
        <BlurText text="Every stream, spike, clip and post — in one place." className="login-headline" delay={60} animateBy="words" direction="top" />
        <p style={{ color: "#D7D2FF", maxWidth: 380, lineHeight: 1.6, margin: 0 }}>
          The system runs on its own. This console lets you watch it, understand every decision it makes, and stop it when you need to.
        </p>
      </div>
      <motion.div className="glass" style={{ width: 260, position: "absolute", left: 44, top: 380, zIndex: 3 }} {...float(0.3)}>
        <div className="row"><span className="k" style={{ margin: 0 }}>Posted last 24h</span><span className="right"><ArrowUpRight size={15} /></span></div>
        <div className="v" style={{ marginTop: 10, fontSize: 28 }}><CountUp to={61} duration={1.6} trigger="load" /></div>
        <div style={{ marginTop: 12 }}><Bars label="Posts per hour" values={[3, 2, 4, 3, 2, 3, 4, 2, 3, 3, 2, 4, 3, 2, 3, 4, 3, 2, 3, 2, 4, 3, 3, 2]} width={220} height={60} /></div>
      </motion.div>
      <motion.div className="glass" style={{ width: 240, position: "absolute", left: 250, top: 580, zIndex: 4, background: "rgba(255,255,255,.18)" }} {...float(0.8)}>
        <div className="row"><span className="livebadge">LIVE</span><span className="k" style={{ margin: 0 }}>Spike → post</span></div>
        <div className="v" style={{ marginTop: 12, fontSize: 28 }}><CountUp to={97} duration={1.8} trigger="load" /> s</div>
        <div className="k" style={{ marginTop: 6 }}>median today · target under 240 s</div>
      </motion.div>
      <motion.div style={{ position: "absolute", right: 70, top: 280, width: 150, zIndex: 2, rotate: 6 }} {...float(0.5)}>
        <Thumb seed="tow-truck" hook="HE TOOK THE TOW TRUCK OFF THE BRIDGE" dur="0:34" live />
      </motion.div>
    </div>
  );
}

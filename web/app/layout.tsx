import type { Metadata, Viewport } from "next";
import { JetBrains_Mono, Plus_Jakarta_Sans } from "next/font/google";

import { Providers } from "@/components/Providers";
import "./globals.css";

const jakarta = Plus_Jakarta_Sans({ variable: "--font-jakarta", subsets: ["latin"], weight: ["400", "500", "600", "700", "800"] });
const jbmono = JetBrains_Mono({ variable: "--font-jbmono", subsets: ["latin"], weight: ["400", "500"] });

export const metadata: Metadata = {
  title: { default: "6 Stars Console", template: "%s · 6 Stars Console" },
  description: "Private control console for the 6 Stars Clips pipeline.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { themeColor: "#5A4AF4", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${jakarta.variable} ${jbmono.variable}`}>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

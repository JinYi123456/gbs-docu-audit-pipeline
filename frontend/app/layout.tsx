import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "SDOC · SI vs BL Verification Console",
  description:
    "Averis x Monash Hackathon 2026 — Intelligent ocean-freight document verification: classify → extract → compare → human escalation.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

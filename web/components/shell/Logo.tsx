/** The VI Stars Clippy badge (public/brand/logo-160.png, 512 px original in logo-512.png). */
export function Logo({ size = 38 }: { size?: number }) {
  return <img src="/brand/logo-160.png" alt="" aria-hidden width={size} height={size} style={{ display: "block", borderRadius: "50%", flex: "none" }} />;
}

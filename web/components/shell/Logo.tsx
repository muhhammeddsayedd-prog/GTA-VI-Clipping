export function Logo({ size = 38 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" aria-hidden>
      <defs>
        <linearGradient id="six-logo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#8C80FF" />
          <stop offset="1" stopColor="#4B3BDF" />
        </linearGradient>
      </defs>
      <path d="M20 3l5 8.7h10L30 20l5 8.3H25L20 37l-5-8.7H5l5-8.3-5-8.3h10z" fill="url(#six-logo)" />
      <path d="M20 12l2.4 4.1h4.8L24.8 20l2.4 3.9h-4.8L20 28l-2.4-4.1h-4.8l2.4-3.9-2.4-3.9h4.8z" fill="#fff" opacity=".9" />
    </svg>
  );
}

import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // Images are short-lived signed Storage URLs and platform CDN photos: next/image would proxy and cache
    // them through Vercel's optimizer (extra cost, and expired signatures cached). Plain <img> is intended.
    rules: { "@next/next/no-img-element": "off" },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Vendored React Bits components (copied from the reactbits.dev registry as-is).
    "components/reactbits/**",
  ]),
]);

export default eslintConfig;

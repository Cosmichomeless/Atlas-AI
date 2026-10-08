import Link from "next/link";

import styles from "./logo.module.css";

/** Marca de Atlas AI: un globo con meridianos sobre un degradado. Decorativa; el nombre lo da el texto. */
export function LogoMark({ size = 28 }: { size?: number }) {
  return (
    <svg
      className={styles.mark}
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id="atlas-mark" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#5b5bf0" />
          <stop offset="1" stopColor="#7c3aed" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#atlas-mark)" />
      <g fill="none" stroke="#fff" strokeWidth="1.7" strokeLinecap="round">
        <circle cx="16" cy="16" r="8" />
        <ellipse cx="16" cy="16" rx="3.6" ry="8" />
        <path d="M8 16h16" />
      </g>
    </svg>
  );
}

/** Marca + nombre, como enlace a `href`. */
export function Brand({ href }: { href: string }) {
  return (
    <Link href={href} className={styles.brand}>
      <LogoMark />
      <span>Atlas AI</span>
    </Link>
  );
}

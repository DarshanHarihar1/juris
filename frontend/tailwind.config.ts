import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
        serif: ["var(--font-serif)", "Georgia", "serif"],
      },
      colors: {
        paper: "#faf9f7", // warm off-white background
        ink: "#1c1917", // near-black text
        muted: "#78716c", // stone-500
        line: "#e7e5e4", // hairline borders
        // verdict accents (5-class)
        verdict: {
          true: "#15803d",
          false: "#dc2626",
          misleading: "#d97706",
          unverifiable: "#6b7280",
          conflicting: "#7c3aed",
        },
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-soft": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
        // rubber-stamp thunk for the verdict mark
        stamp: {
          "0%": { opacity: "0", transform: "scale(1.8) rotate(-10deg)" },
          "60%": { opacity: "1", transform: "scale(0.94) rotate(-2deg)" },
          "100%": { opacity: "1", transform: "scale(1) rotate(-3deg)" },
        },
        // indeterminate progress sweep
        sweep: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(400%)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.4s ease-out both",
        "pulse-soft": "pulse-soft 1.4s ease-in-out infinite",
        stamp: "stamp 0.35s cubic-bezier(0.2, 0.8, 0.3, 1.1) both",
        sweep: "sweep 1.6s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
export default config;

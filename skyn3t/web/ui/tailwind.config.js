/** @type {import('tailwindcss').Config} */
// Tailwind config aligned with the atelier palette already in the
// backend's :root tokens. The SPA reuses the same warm-amber accent
// + graphite surfaces so the new UI looks like the same product, just
// not fighting itself.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx,js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          0: "#0f0e0c",
          1: "#16140f",
          2: "#1d1a13",
          3: "#25211a",
        },
        border: {
          DEFAULT: "#2d2920",
          strong: "#3b362a",
        },
        text: {
          primary: "#f4ede0",
          secondary: "#b8ad96",
          dim: "#7a705f",
        },
        accent: {
          DEFAULT: "#c9a96e",
          strong: "#d9b97e",
          soft:   "rgba(201, 169, 110, 0.08)",
          line:   "rgba(201, 169, 110, 0.22)",
        },
        status: {
          green:  "#8aa37a",
          yellow: "#d4a64a",
          red:    "#b65a4a",
        },
      },
      fontFamily: {
        display: ['"Instrument Serif"', "Georgia", "serif"],
        sans:    ['"Space Grotesk"', "system-ui", "sans-serif"],
        mono:    ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};

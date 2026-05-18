/** @type {import('tailwindcss').Config} */
// Tailwind config aligned with the Tactical Ops (warm) machine-room palette.
// Deep graphite browns + ember-orange accent. Fonts: Orbitron (display),
// Rajdhani (body), JetBrains Mono (data). This is the "warm atelier"
// aesthetic — a server closet that has been humming in the dark for years.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx,js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          0: "#0F0D0A",
          1: "#1A1714",
          2: "#24201A",
          3: "#2E2920",
        },
        border: {
          DEFAULT: "#3B3429",
          strong: "#4A4337",
        },
        text: {
          primary: "#E8DDCB",
          secondary: "#8C8270",
          dim: "#5C5448",
        },
        accent: {
          DEFAULT: "#E05C1A",
          strong: "#F06E2E",
          soft: "rgba(224, 92, 26, 0.08)",
          line: "rgba(224, 92, 26, 0.22)",
        },
        status: {
          green: "#7A9E6A",
          yellow: "#C49A3A",
          red: "#B05040",
        },
      },
      fontFamily: {
        display: ['"Orbitron"', "system-ui", "sans-serif"],
        sans: ['"Rajdhani"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};

/** @type {import('tailwindcss').Config} */
// Command-center atelier: Instrument Serif display, Space Grotesk UI,
// cyan live-data accent + amber warmth on deep charcoal.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx,js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          0: "#0c0e12",
          1: "#111520",
          2: "#171d28",
          3: "#1f2735",
        },
        border: {
          DEFAULT: "#2a3344",
          strong: "#3a4558",
        },
        text: {
          primary: "#e4eaf0",
          secondary: "#8a9bb0",
          dim: "#566577",
        },
        accent: {
          DEFAULT: "#38d4f0",
          strong: "#5ee4ff",
          soft: "rgba(56, 212, 240, 0.08)",
          line: "rgba(56, 212, 240, 0.24)",
        },
        amber: {
          DEFAULT: "#e5a045",
          strong: "#f0b45c",
          soft: "rgba(229, 160, 69, 0.10)",
          line: "rgba(229, 160, 69, 0.28)",
        },
        chrome: {
          DEFAULT: "#9aa8b8",
          dim: "#6b7a8c",
          bright: "#d0d8e2",
        },
        status: {
          green: "#4ecf9a",
          yellow: "#e5b84a",
          red: "#f07171",
        },
      },
      fontFamily: {
        display: ['"Instrument Serif"', "Georgia", "serif"],
        sans: ['"Space Grotesk"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      boxShadow: {
        glow: "0 0 24px rgba(56, 212, 240, 0.30)",
        "glow-sm": "0 0 12px rgba(56, 212, 240, 0.22)",
        panel: "0 20px 50px -24px rgba(0, 0, 0, 0.65)",
      },
      animation: {
        "live-pulse": "live-pulse 1.4s ease-in-out infinite",
        "atelier-rise": "atelier-rise 480ms cubic-bezier(0.2, 0.7, 0.2, 1) both",
      },
      keyframes: {
        "live-pulse": {
          "0%, 100%": { opacity: "1", boxShadow: "0 0 8px rgba(56, 212, 240, 0.6)" },
          "50%": { opacity: "0.35", boxShadow: "0 0 4px rgba(56, 212, 240, 0.2)" },
        },
        "atelier-rise": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
};

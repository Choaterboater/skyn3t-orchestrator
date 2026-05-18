/** @type {import('tailwindcss').Config} */
// Tailwind config aligned with the SkyN3t / ChoateLabs brand:
// cyan-on-black with brushed silver chrome and a cyan glow halo.
// Fonts: Orbitron (display headings), Rajdhani (body), JetBrains Mono (data).
// The aesthetic is "autonomous machine / circuit luxe" — taken from the
// canonical logo at data/design_references/canonical_brand.png.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx,js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          0: "#000000",   // pure black — matches logo
          1: "#070A0E",   // near-black slab
          2: "#0E141B",   // chrome shadow
          3: "#16202B",   // raised panel
        },
        border: {
          DEFAULT: "#1E2A38",
          strong: "#2B3D52",
        },
        text: {
          primary: "#E6F6FA",
          secondary: "#8FA8B5",
          dim: "#56717F",
        },
        accent: {
          DEFAULT: "#0FF0FC",        // cyan glow — primary accent
          strong: "#26F5FF",         // hover/active state, slightly brighter
          soft: "rgba(15, 240, 252, 0.08)",
          line: "rgba(15, 240, 252, 0.24)",
        },
        chrome: {
          DEFAULT: "#A8B0B8",        // brushed silver — bezel highlights
          dim: "#6F7A85",
          bright: "#D7DCE2",
        },
        status: {
          green: "#3DDC97",
          yellow: "#FFCB47",
          red: "#FF5C5C",
        },
      },
      fontFamily: {
        display: ['"Orbitron"', "system-ui", "sans-serif"],
        sans: ['"Rajdhani"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      boxShadow: {
        glow: "0 0 24px rgba(15, 240, 252, 0.35)",
        "glow-sm": "0 0 12px rgba(15, 240, 252, 0.25)",
      },
    },
  },
  plugins: [],
};

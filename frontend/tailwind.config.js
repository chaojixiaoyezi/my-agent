/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: "#FAFAF8",
        card: "#FFFFFF",
        ink: "#2D2D2D",
        "ink-secondary": "#6B6B6B",
        "ink-tertiary": "#9CA3AF",
        border: "rgba(0,0,0,0.06)",
        "border-hover": "rgba(0,0,0,0.10)",
        "accent-blue": "#60A5FA",
        "accent-blue-dark": "#3B82F6",
        "accent-green": "#34D399",
        "accent-green-dark": "#10B981",
        "accent-orange": "#FB923C",
        "accent-orange-dark": "#F97316",
        "accent-red": "#F87171",
        "accent-red-dark": "#EF4444",
        "accent-purple": "#A78BFA",
        "accent-cyan": "#22D3EE",
      },
      borderRadius: {
        "2xl": "12px",
        "3xl": "16px",
        pill: "9999px",
      },
      boxShadow: {
        card: "0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.02)",
        "card-hover": "0 2px 6px rgba(0,0,0,0.05), 0 8px 20px rgba(0,0,0,0.04)",
        focus: "0 0 0 3px rgba(96,165,250,0.15)",
      },
      transitionTimingFunction: {
        bounce: "cubic-bezier(0.34, 1.56, 0.64, 1)",
        smooth: "cubic-bezier(0.4, 0, 0.2, 1)",
      },
      transitionDuration: {
        150: "150ms",
        200: "200ms",
        250: "250ms",
        300: "300ms",
      },
    },
  },
  plugins: [],
}

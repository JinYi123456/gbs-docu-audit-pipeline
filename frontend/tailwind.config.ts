import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 与人审语义绑定的语义色：红=缺陷 / 黄=待判 / 绿=一致 / 蓝=升级
        defect: { bg: "#fef2f2", border: "#fecaca", text: "#b91c1c" },
        undecided: { bg: "#fffbeb", border: "#fde68a", text: "#b45309" },
        ok: { bg: "#f0fdf4", border: "#bbf7d0", text: "#15803d" },
        escalate: { bg: "#eff6ff", border: "#bfdbfe", text: "#1d4ed8" },
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;

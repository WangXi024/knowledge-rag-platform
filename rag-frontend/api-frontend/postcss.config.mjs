// PostCSS 只启用 Tailwind v4 插件，把 globals.css 中的工具类转换为最终 CSS。
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;

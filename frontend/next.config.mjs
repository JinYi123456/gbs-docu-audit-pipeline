/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // 工作台只读本快照文件，不做服务端外部请求；关掉 telemetry 提示噪音
  eslint: { ignoreDuringBuilds: true },
  experimental: { },
};

export default nextConfig;

import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  output: 'standalone',
  poweredByHeader: false,
  reactStrictMode: true,
  generateBuildId: async () => process.env.LOOPCAST_BUILD_ID ?? 'local-build',
};

export default nextConfig;

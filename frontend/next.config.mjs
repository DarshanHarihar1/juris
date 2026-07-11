/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async redirects() {
    // /landing was the old home for the marketing page; it now lives at /.
    return [{ source: "/landing", destination: "/", permanent: true }];
  },
};
export default nextConfig;

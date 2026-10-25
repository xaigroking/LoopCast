import type { Metadata, Viewport } from 'next';
import Script from 'next/script';
import './globals.css';
import { WorkspaceProvider } from '@/lib/workspace';
import { WorkspaceShell } from '@/components/shell';

export const metadata: Metadata = {
  title: 'LoopCast',
  description: 'Local task history and publication evidence simulator.',
  icons: {
    icon: [16, 24, 32].map((size) => ({ url: `/brand.svg?size=${size}`, sizes: `${size}x${size}`, type: 'image/svg+xml' })),
  },
};

export const viewport: Viewport = { width: 'device-width', initialScale: 1 };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" dir="ltr" suppressHydrationWarning>
    <head><Script src="/preferences.js" strategy="beforeInteractive" /></head>
    <body><WorkspaceProvider><WorkspaceShell>{children}</WorkspaceShell></WorkspaceProvider></body>
  </html>;
}

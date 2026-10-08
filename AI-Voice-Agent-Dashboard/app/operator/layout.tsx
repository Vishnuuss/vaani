import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'Operations',
  // Keep it out of search engines and out of the client's browser history
  // suggestions. The real protection is the server-side key check; this just
  // avoids advertising that the console exists.
  robots: { index: false, follow: false, nocache: true },
};

export default function OperatorLayout({ children }: { children: React.ReactNode }) {
  return <div className="min-h-screen bg-background">{children}</div>;
}
